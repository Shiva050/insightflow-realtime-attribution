"""
Local tests for the CRM enrichment handler.

No AWS, no network. The DynamoDB fake enforces the two ConditionExpressions we
actually rely on, because the atomic claim is the whole idempotency story — a
fake that rubber-stamps conditional writes would test nothing.

    python3 tests/test_enrich.py
"""

import importlib.util
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")


def load_handler(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))
from fakes import FakeDynamo, FakeS3  # noqa: E402


RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    line = f"  [{mark}] {name}"
    if detail and not condition:
        line += f"\n         {detail}"
    print(line)


def read_fixture(filename):
    with open(os.path.join(FIXTURES, filename)) as fh:
        return fh.read()


BRONZE_KEY = "crm/events/dt=2025-05-20/crm_event_ev_1ntH1vAE4G7DNjNZYkMeck.json"
LEAD_ID = "lead_niuYPXlw6vnFQIhwZCKDaaKv9XMQs9KA5NgxhNRBgaA"
EVENT_ID = "ev_1ntH1vAE4G7DNjNZYkMeck"

OWNER_FILE = {
    "lead_id": LEAD_ID,
    "display_name": "Test Lead",
    "lead_email": "test.lead@example.com",
    "lead_owner": "Test Owner",
    "funnel": "DE ACADEMY Direct VSL",
    "status_label": "Potential",
    "date_created": "2025-05-20T12:14:56.409000+00:00",
}


def sqs_event(key=BRONZE_KEY, message_id="msg-1"):
    """An SQS record whose body is an S3 event notification."""
    return {
        "Records": [
            {
                "messageId": message_id,
                "body": json.dumps(
                    {
                        "Records": [
                            {
                                "s3": {
                                    "bucket": {"name": "insightflow-bronze"},
                                    "object": {"key": key},
                                }
                            }
                        ]
                    }
                ),
            }
        ]
    }


def fresh_module():
    """Load the handler with fakes wired in and Slack in dry-run."""
    mod = load_handler(
        "crm_enrich",
        os.path.join(REPO_ROOT, "lambdas", "crm_enrich", "lambda_function.py"),
    )
    mod.dynamodb = FakeDynamo()
    mod.s3 = FakeS3({BRONZE_KEY: read_fixture("crm_event_created.json")})
    mod.DRY_RUN = True
    mod.sent_messages = []

    def fake_post(message):
        mod.sent_messages.append(message)

    mod.post_to_slack = fake_post
    return mod


def test_happy_path_owner_present():
    print("\nEnrichment - owner available")
    mod = fresh_module()
    mod.owner_from_source = lambda lead_id: dict(OWNER_FILE)

    result = mod.lambda_handler(sqs_event(), None)

    check("no batch failures", result["batchItemFailures"] == [])
    check("alert sent", len(mod.sent_messages) == 1)

    text = mod.sent_messages[0]["text"] if mod.sent_messages else ""
    check("alert carries all seven spec fields",
          all(f in text for f in ("Name:", "Lead ID:", "Created Date:", "Label:",
                                  "Email:", "Lead Owner:", "Funnel:")))
    check("owner from lookup appears in alert", "Test Owner" in text)
    check("email from lookup appears in alert", "test.lead@example.com" in text)

    ledger = mod.dynamodb.tables[mod.LEDGER_TABLE]
    check("ledger marked SENT", ledger[EVENT_ID]["status"]["S"] == "SENT")

    cache = mod.dynamodb.tables.get(mod.OWNER_CACHE_TABLE, {})
    check("owner cached for later events (D14)", LEAD_ID in cache)

    awaiting = mod.dynamodb.tables.get(mod.AWAITING_TABLE, {})
    check("nothing on the awaiting worklist", not awaiting)


def test_owner_missing():
    print("\nEnrichment - owner not yet assigned")
    mod = fresh_module()
    mod.owner_from_source = lambda lead_id: None

    mod.lambda_handler(sqs_event(), None)

    text = mod.sent_messages[0]["text"] if mod.sent_messages else ""
    check("alert still sent (never blocked on a missing owner)",
          len(mod.sent_messages) == 1)
    check("owner rendered as pending, not fabricated", "_pending assignment_" in text)

    awaiting = mod.dynamodb.tables.get(mod.AWAITING_TABLE, {})
    check("lead placed on awaiting-owner worklist (D15)", LEAD_ID in awaiting)
    check("worklist row starts at retry_count 0",
          awaiting.get(LEAD_ID, {}).get("retry_count", {}).get("N") == "0")

    cache = mod.dynamodb.tables.get(mod.OWNER_CACHE_TABLE, {})
    check("null owner NOT negative-cached (D15)", LEAD_ID not in cache)

    ledger = mod.dynamodb.tables[mod.LEDGER_TABLE]
    check("ledger still marked SENT", ledger[EVENT_ID]["status"]["S"] == "SENT")


def test_idempotency():
    print("\nIdempotency - the atomic claim (D10)")
    mod = fresh_module()
    mod.owner_from_source = lambda lead_id: dict(OWNER_FILE)

    mod.lambda_handler(sqs_event(message_id="msg-1"), None)
    mod.lambda_handler(sqs_event(message_id="msg-2"), None)

    check("redelivery does not re-alert", len(mod.sent_messages) == 1,
          f"{len(mod.sent_messages)} alerts sent")

    # Two workers racing on a fresh event: exactly one may win.
    mod2 = fresh_module()
    first = mod2.claim_event("ev_race")
    second = mod2.claim_event("ev_race")
    check("concurrent claims - exactly one winner", first is True and second is False)

    # A crashed claimer must not strand the alert forever (D11).
    mod3 = fresh_module()
    mod3.claim_event("ev_stuck")
    stuck = mod3.dynamodb.tables[mod3.LEDGER_TABLE]["ev_stuck"]
    stuck["claimed_at"] = {"N": str(int(time.time()) - mod3.LEASE_SECONDS - 60)}
    check("expired PENDING lease is reclaimable (D11)",
          mod3.claim_event("ev_stuck") is True)

    # A SENT event is never reclaimable, however old.
    mod4 = fresh_module()
    mod4.claim_event("ev_done")
    mod4.mark_sent("ev_done")
    done = mod4.dynamodb.tables[mod4.LEDGER_TABLE]["ev_done"]
    done["claimed_at"] = {"N": "0"}
    check("SENT event never reclaimed regardless of age",
          mod4.claim_event("ev_done") is False)


def test_ordering_and_failure():
    print("\nOrdering and failure handling (D12, D13)")

    # Slack fails => ledger must NOT say SENT, and the message must be retried.
    mod = fresh_module()
    mod.owner_from_source = lambda lead_id: dict(OWNER_FILE)

    def failing_post(message):
        raise RuntimeError("slack is down")

    mod.post_to_slack = failing_post
    result = mod.lambda_handler(sqs_event(message_id="msg-fail"), None)

    ledger = mod.dynamodb.tables[mod.LEDGER_TABLE]
    check("failed send leaves ledger PENDING, not SENT (D12)",
          ledger[EVENT_ID]["status"]["S"] == "PENDING")
    check("failed message reported for redelivery",
          result["batchItemFailures"] == [{"itemIdentifier": "msg-fail"}])

    # And after the lease expires the retry can proceed rather than deadlock.
    ledger[EVENT_ID]["claimed_at"] = {"N": str(int(time.time()) - mod.LEASE_SECONDS - 60)}
    mod.post_to_slack = lambda message: mod.sent_messages.append(message)
    mod.lambda_handler(sqs_event(message_id="msg-retry"), None)
    check("retry after lease expiry delivers the alert", len(mod.sent_messages) == 1)
    check("ledger SENT after successful retry",
          ledger[EVENT_ID]["status"]["S"] == "SENT")

    # A worklist row must never have its counters reset by a redelivery.
    mod2 = fresh_module()
    mod2.record_awaiting_owner(LEAD_ID, EVENT_ID)
    row = mod2.dynamodb.tables[mod2.AWAITING_TABLE][LEAD_ID]
    row["retry_count"] = {"N": "7"}
    row["first_seen_at"] = {"N": "1000"}
    mod2.record_awaiting_owner(LEAD_ID, EVENT_ID)
    after = mod2.dynamodb.tables[mod2.AWAITING_TABLE][LEAD_ID]
    check("re-adding to worklist preserves retry_count (D17 stays reachable)",
          after["retry_count"]["N"] == "7" and after["first_seen_at"]["N"] == "1000")


def test_message_parsing():
    print("\nMessage handling")
    mod = fresh_module()
    mod.owner_from_source = lambda lead_id: dict(OWNER_FILE)

    # S3 notifications URL-encode keys; failing to unquote yields NoSuchKey.
    encoded = BRONZE_KEY.replace("=", "%3D")
    mod.lambda_handler(sqs_event(key=encoded, message_id="msg-enc"), None)
    check("URL-encoded S3 key is unquoted before the GET",
          len(mod.sent_messages) == 1)

    # An object missing identity fields is unprocessable, not retryable.
    mod2 = fresh_module()
    mod2.s3 = FakeS3({"bad.json": json.dumps({"event": {"id": "ev_x"}})})
    result = mod2.lambda_handler(sqs_event(key="bad.json", message_id="msg-bad"), None)
    check("event without lead_id is skipped, not retried",
          result["batchItemFailures"] == [] and not mod2.sent_messages)

    # One bad message must not drag healthy ones back onto the queue.
    mod3 = fresh_module()
    mod3.owner_from_source = lambda lead_id: dict(OWNER_FILE)
    batch = {
        "Records": [
            sqs_event(message_id="ok-1")["Records"][0],
            {"messageId": "broken", "body": "not json"},
        ]
    }
    result = mod3.lambda_handler(batch, None)
    check("partial batch failure isolates the bad message",
          result["batchItemFailures"] == [{"itemIdentifier": "broken"}]
          and len(mod3.sent_messages) == 1)


def test_cache_behaviour():
    print("\nRead-through cache (D14)")
    mod = fresh_module()
    calls = []

    def counting_source(lead_id):
        calls.append(lead_id)
        return dict(OWNER_FILE)

    mod.owner_from_source = counting_source

    mod.resolve_owner(LEAD_ID, "created")
    mod.resolve_owner(LEAD_ID, "updated")
    mod.resolve_owner(LEAD_ID, "updated")
    check("foreign bucket read once per lead, not per event",
          len(calls) == 1, f"{len(calls)} source reads")

    # The flagged assumption: 'updated implies already cached' is false when the
    # creation event never landed. An update on a cold cache must still resolve.
    mod2 = fresh_module()
    mod2.owner_from_source = lambda lead_id: dict(OWNER_FILE)
    owner = mod2.resolve_owner("lead_never_seen", "updated")
    check("cold cache on an 'updated' event still reads the source",
          owner is not None and owner.get("lead_owner") == "Test Owner")


if __name__ == "__main__":
    test_happy_path_owner_present()
    test_owner_missing()
    test_idempotency()
    test_ordering_and_failure()
    test_message_parsing()
    test_cache_behaviour()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
