#!/usr/bin/env python3
"""
Replay synthetic CRM webhooks at the deployed endpoint.

Registration of the real Close and Calendly subscriptions is owned by the SMEs,
so this exists to exercise the full path without it:

    API Gateway -> ingest Lambda -> S3 -> S3 notification -> SQS (10 min delay)
    -> enrichment Lambda -> owner lookup -> Slack -> ledger

It is also the better dev loop even after registration lands — waiting for a real
lead to trickle in is a poor way to test a change.

Examples
--------
Print a payload without sending it:
    python3 tools/replay.py --dry-run

Send one synthetic lead (its owner will 404, exercising the awaiting-owner
worklist and the reconciliation sweep):
    python3 tools/replay.py --url https://{api-id}.execute-api.us-east-1.amazonaws.com/deploy/crm

Send a lead that has a real owner file, exercising the enrichment happy path.
Use a lead_id you know exists in the dea-lead-owner bucket:
    python3 tools/replay.py --url ... --lead-id lead_XXXXXXXX

Prove idempotency — the same event twice must yield ONE S3 object and ONE alert:
    python3 tools/replay.py --url ... --repeat 2

Simulate concurrent delivery, which is what the atomic claim (D10) defends
against:
    python3 tools/replay.py --url ... --repeat 5 --concurrent
"""

import argparse
import concurrent.futures
import json
import random
import string
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone


def random_suffix(n=22):
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def build_event(lead_id=None, event_id=None, action="created", display_name=None):
    """
    Construct a webhook body matching the shape Close actually sends.

    Defaults generate a lead_id that will NOT exist in the public owner bucket,
    which is the more interesting path: the alert must still fire with a blank
    owner, the lead must land on the awaiting-owner worklist, and the null must
    never be cached (D15).
    """
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f")

    lead_id = lead_id or f"lead_{random_suffix(43)}"
    event_id = event_id or f"ev_{random_suffix(22)}"
    display_name = display_name or f"Replay Lead {uuid.uuid4().hex[:8]}"

    return {
        "subscription_id": "whsub_REPLAY000000000000000",
        "event": {
            "id": event_id,
            "date_created": stamp,
            "date_updated": stamp,
            "organization_id": "orga_REPLAY0000000000000000000000000000000000",
            "user_id": "user_REPLAY0000000000000000000000000000000000",
            "request_id": f"req_{random_suffix(22)}",
            "api_key_id": None,
            "oauth_client_id": "oa2client_REPLAY000000000",
            "oauth_scope": "all.full_access offline_access",
            "object_type": "lead",
            "object_id": lead_id,
            "lead_id": lead_id,
            "action": action,
            "changed_fields": [] if action == "created" else ["status_label"],
            "meta": {"request_path": "/api/v1/lead/", "request_method": "POST"},
            "data": {
                "id": lead_id,
                "display_name": display_name,
                "name": "",
                "description": "",
                "url": None,
                "addresses": [],
                "contact_ids": [f"cont_{random_suffix(43)}"],
                "status_label": "Potential",
                "status_id": "stat_REPLAY0000000000000000000000000000000000",
                "date_created": f"{stamp}+00:00",
                "date_updated": f"{stamp}+00:00",
                "organization_id": "orga_REPLAY0000000000000000000000000000000000",
                "created_by": "user_REPLAY0000000000000000000000000000000000",
                "created_by_name": "Replay Harness",
                "updated_by": "user_REPLAY0000000000000000000000000000000000",
                "updated_by_name": "Replay Harness",
            },
            "previous_data": {},
        },
    }


def send(url, body, timeout=10):
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "InsightFlow Replay"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")
    except urllib.error.URLError as exc:
        return None, str(exc.reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="Ingest endpoint, e.g. https://.../deploy/crm")
    parser.add_argument("--count", type=int, default=1,
                        help="Number of distinct leads to send (default 1)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="Times to send EACH event. >1 tests idempotency: "
                             "the ledger must still produce one alert.")
    parser.add_argument("--concurrent", action="store_true",
                        help="Send repeats simultaneously to exercise the atomic claim")
    parser.add_argument("--lead-id",
                        help="Use a specific lead_id. Supply one that exists in the "
                             "dea-lead-owner bucket to test the enrichment happy path.")
    parser.add_argument("--action", default="created", choices=["created", "updated"])
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the payload instead of sending it")
    args = parser.parse_args()

    if not args.url and not args.dry_run:
        parser.error("--url is required unless --dry-run is set")

    events = [
        build_event(lead_id=args.lead_id, action=args.action)
        for _ in range(args.count)
    ]

    if args.dry_run:
        for body in events:
            print(json.dumps(body, indent=2))
        print(f"\n{len(events)} event(s) generated, none sent (--dry-run)",
              file=sys.stderr)
        return 0

    failures = 0
    for body in events:
        event_id = body["event"]["id"]
        lead_id = body["event"]["lead_id"]
        print(f"\nevent_id={event_id}\nlead_id={lead_id}")

        if args.concurrent and args.repeat > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.repeat) as pool:
                results = list(pool.map(lambda _: send(args.url, body),
                                        range(args.repeat)))
        else:
            results = [send(args.url, body) for _ in range(args.repeat)]

        for i, (status, text) in enumerate(results, 1):
            ok = status == 200
            failures += 0 if ok else 1
            print(f"  send {i}/{len(results)}: {status or 'ERROR'} {text.strip()[:160]}")

    print(f"\n{len(events)} lead(s) x {args.repeat} send(s); {failures} failure(s)")

    if args.repeat > 1:
        print(
            "\nExpected result: ONE S3 object per event_id (the key is a pure "
            "function of the payload) and ONE Slack alert per event (the ledger's "
            "conditional claim rejects the duplicates)."
        )
    print("Alerts arrive ~10 minutes later — the SQS delay queue is holding them.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
