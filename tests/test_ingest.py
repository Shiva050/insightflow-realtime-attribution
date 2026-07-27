"""
Local tests for the Bronze ingest handlers.

No AWS, no pytest, no network — the S3 client is replaced with a recorder so we
can assert on the exact key each handler derives. Run with:

    python3 tests/test_ingest.py

The point of these is the object key. The key IS the dedup mechanism (D3, C2),
so a silent change in how it is derived is a silent duplicate-data bug.
"""

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")


def load_handler(name, path):
    """Import a Lambda module by file path (they are not a package)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeS3:
    """Records put_object calls; can be told to fail."""

    def __init__(self, fail=False):
        self.puts = []
        self.fail = fail

    def put_object(self, **kwargs):
        if self.fail:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "boom"}}, "PutObject"
            )
        self.puts.append(kwargs)
        return {"ETag": '"fake"'}


def read_fixture(filename):
    with open(os.path.join(FIXTURES, filename)) as fh:
        return fh.read()


def api_gw_event(body, headers=None, b64=False):
    """Wrap a raw webhook body in an API Gateway proxy event."""
    if b64:
        body = base64.b64encode(body.encode()).decode()
    return {
        "resource": "/crm",
        "httpMethod": "POST",
        "headers": headers or {"Content-Type": "application/json"},
        "body": body,
        "isBase64Encoded": b64,
    }


RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    line = f"  [{mark}] {name}"
    if detail and not condition:
        line += f"\n         {detail}"
    print(line)


# ---------------------------------------------------------------------------
# CRM ingest
# ---------------------------------------------------------------------------
def test_crm():
    print("\nCRM ingest")
    crm = load_handler(
        "crm_ingest", os.path.join(REPO_ROOT, "lambdas", "crm_ingest", "lambda_function.py")
    )
    # Pin the key cache so no test ever reaches for SSM. CI has no credentials,
    # and an unpinned cache would make every case pay a boto retry timeout.
    crm._signing_key_cache = ""
    body = read_fixture("crm_event_created.json")

    # --- happy path: key derived from event_id, dt from payload date_created
    fake = FakeS3()
    crm.s3 = fake
    resp = crm.lambda_handler(api_gw_event(body), None)
    expected = "crm/events/dt=2025-05-20/crm_event_ev_1ntH1vAE4G7DNjNZYkMeck.json"
    actual = fake.puts[0]["Key"] if fake.puts else None
    check("200 accepted", resp["statusCode"] == 200, f"got {resp['statusCode']}")
    check("key is event_id-based, dt from payload", actual == expected,
          f"expected {expected}\n         actual   {actual}")

    # --- the whole point of D3: a retry must overwrite, not duplicate
    crm.lambda_handler(api_gw_event(body), None)
    keys = {p["Key"] for p in fake.puts}
    check("retry regenerates the identical key (idempotent overwrite)",
          len(fake.puts) == 2 and len(keys) == 1,
          f"{len(fake.puts)} puts across {len(keys)} distinct keys")

    # --- stored as one compact line, values intact
    stored = fake.puts[0]["Body"].decode("utf-8")
    check("stored as a single line (Athena's SerDe is line-oriented)",
          "\n" not in stored)
    check("every value preserved through reframing",
          json.loads(stored) == json.loads(body))

    # --- base64 transport
    fake = FakeS3()
    crm.s3 = fake
    resp = crm.lambda_handler(api_gw_event(body, b64=True), None)
    check("base64-encoded body handled", resp["statusCode"] == 200 and fake.puts)

    # --- malformed input is a client error, not a retry loop
    fake = FakeS3()
    crm.s3 = fake
    resp = crm.lambda_handler(api_gw_event("this is not json"), None)
    check("unparseable body -> 400", resp["statusCode"] == 400)

    no_id = json.dumps({"event": {"date_created": "2025-05-20T12:14:56"}})
    resp = crm.lambda_handler(api_gw_event(no_id), None)
    check("missing event.id -> 400", resp["statusCode"] == 400)

    no_date = json.dumps({"event": {"id": "ev_x"}})
    resp = crm.lambda_handler(api_gw_event(no_date), None)
    check("missing date_created -> 400", resp["statusCode"] == 400)
    check("nothing written on bad input", not fake.puts)

    # --- transient failure must be retryable
    crm.s3 = FakeS3(fail=True)
    resp = crm.lambda_handler(api_gw_event(body), None)
    check("S3 failure -> 500 so Close retries", resp["statusCode"] == 500)

    # --- signature verification, once a key exists
    #
    # Close issues signature_key as a HEX STRING and signs with its DECODED
    # bytes (verified against developer.close.com, 2026-07-26). The key below is
    # hex on purpose: signing with the hex TEXT is the bug this suite now guards.
    key_hex = "058bfb6a3d8cfdc4da7c3be5901b16ae11da982b46a25fb2cd7016e97a140a1c"
    key_bytes = bytes.fromhex(key_hex)
    crm._signing_key_cache = key_hex
    crm.s3 = FakeS3()
    ts = str(int(time.time()))
    good = hmac.new(key_bytes, f"{ts}{body}".encode(), hashlib.sha256).hexdigest()
    resp = crm.lambda_handler(
        api_gw_event(body, {"Close-Sig-Hash": good, "Close-Sig-Timestamp": ts}), None
    )
    check("valid signature accepted", resp["statusCode"] == 200)

    # REGRESSION GUARD. Signing with the hex text instead of the decoded bytes
    # produces a plausible-looking digest that Close would never send. If this
    # ever returns 200 the key is being used as raw UTF-8 again, and every real
    # webhook would 401 in production.
    wrong = hmac.new(
        key_hex.encode("utf-8"), f"{ts}{body}".encode(), hashlib.sha256
    ).hexdigest()
    resp = crm.lambda_handler(
        api_gw_event(body, {"Close-Sig-Hash": wrong, "Close-Sig-Timestamp": ts}), None
    )
    check("hex key must be decoded, not UTF-8 encoded -> 401", resp["statusCode"] == 401)

    resp = crm.lambda_handler(
        api_gw_event(body, {"Close-Sig-Hash": "deadbeef", "Close-Sig-Timestamp": ts}), None
    )
    check("bad signature -> 401", resp["statusCode"] == 401)

    old_ts = str(int(time.time()) - 3600)
    stale = hmac.new(key_bytes, f"{old_ts}{body}".encode(), hashlib.sha256).hexdigest()
    resp = crm.lambda_handler(
        api_gw_event(body, {"Close-Sig-Hash": stale, "Close-Sig-Timestamp": old_ts}), None
    )
    check("replayed old signature -> 401", resp["statusCode"] == 401)

    # headers are case-insensitive over the wire
    crm.s3 = FakeS3()
    resp = crm.lambda_handler(
        api_gw_event(body, {"close-sig-hash": good, "close-sig-timestamp": ts}), None
    )
    check("header lookup is case-insensitive", resp["statusCode"] == 200)

    # a non-hex key is a configuration error, not an accept
    crm._signing_key_cache = "not-hex-at-all"
    resp = crm.lambda_handler(
        api_gw_event(body, {"Close-Sig-Hash": good, "Close-Sig-Timestamp": ts}), None
    )
    check("non-hex signing key -> 401", resp["statusCode"] == 401)

    # --- fail-closed switch
    # Before registration an absent key is tolerated; after, it must not be.
    crm._signing_key_cache = ""
    crm.s3 = FakeS3()
    resp = crm.lambda_handler(api_gw_event(body), None)
    check("no key + REQUIRE_SIGNATURE off -> accepted", resp["statusCode"] == 200)

    crm.REQUIRE_SIGNATURE = True
    resp = crm.lambda_handler(api_gw_event(body), None)
    check("no key + REQUIRE_SIGNATURE on -> 401", resp["statusCode"] == 401)

    crm.REQUIRE_SIGNATURE = False
    crm._signing_key_cache = ""  # restore unverified default


# ---------------------------------------------------------------------------
# Calendly ingest
# ---------------------------------------------------------------------------
def test_calendly():
    print("\nCalendly ingest")
    cal = load_handler(
        "calendly_ingest",
        os.path.join(REPO_ROOT, "lambdas", "calendly_ingest", "lambda_function.py"),
    )
    cal._signing_key_cache = ""  # see test_crm
    body = read_fixture("calendly_invitee_created.json")

    fake = FakeS3()
    cal.s3 = fake
    resp = cal.lambda_handler(api_gw_event(body), None)
    expected = (
        "calendly/bookings/dt=2025-07-09/"
        "calendly_event_22a0f2d6-1bde-4fc1-95c1-d969df1da21d.json"
    )
    actual = fake.puts[0]["Key"] if fake.puts else None
    check("200 accepted", resp["statusCode"] == 200)
    check("key is invitee-uuid only (C2)", actual == expected,
          f"expected {expected}\n         actual   {actual}")

    # C2's real payoff: same invitee rescheduling to a NEW scheduled_event must
    # still collapse to one object. A composite key would land two.
    rescheduled = json.loads(body)
    rescheduled["payload"]["scheduled_event"]["uri"] = (
        "https://api.calendly.com/scheduled_events/ffffffff-0000-0000-0000-000000000000"
    )
    cal.lambda_handler(api_gw_event(json.dumps(rescheduled)), None)
    keys = {p["Key"] for p in fake.puts}
    check("reschedule (new scheduled_event) reuses the same key",
          len(keys) == 1, f"{len(keys)} distinct keys: {keys}")

    # Multi-invitee meeting: 3 invitees on one meeting => 3 objects (C3).
    fake = FakeS3()
    cal.s3 = fake
    for uuid in ("aaaa1111-0000-0000-0000-000000000001",
                 "bbbb2222-0000-0000-0000-000000000002",
                 "cccc3333-0000-0000-0000-000000000003"):
        multi = json.loads(body)
        multi["payload"]["uri"] = (
            "https://api.calendly.com/scheduled_events/"
            "1ac9e88e-eae3-4e4b-b979-d770cff02d72/invitees/" + uuid
        )
        cal.lambda_handler(api_gw_event(json.dumps(multi)), None)
    check("3 invitees on one meeting -> 3 objects (C3)",
          len({p["Key"] for p in fake.puts}) == 3)

    # Org-wide subscription: unmapped event_types are still landed (C5).
    fake = FakeS3()
    cal.s3 = fake
    unmapped = json.loads(body)
    unmapped["payload"]["scheduled_event"]["event_type"] = (
        "https://api.calendly.com/event_types/99999999-9999-9999-9999-999999999999"
    )
    resp = cal.lambda_handler(api_gw_event(json.dumps(unmapped)), None)
    check("unmapped event_type still landed (C5 - no filtering in Bronze)",
          resp["statusCode"] == 200 and len(fake.puts) == 1)

    # --- cancellations -----------------------------------------------------
    # A cancellation is a separate event about the same invitee. It must land
    # under its own prefix; keyed identically it would overwrite the creation
    # and destroy the booking record.
    fake = FakeS3()
    cal.s3 = fake
    canceled = json.loads(body)
    canceled["event"] = "invitee.canceled"
    canceled["payload"]["status"] = "canceled"
    canceled["payload"]["updated_at"] = "2025-07-11T09:30:00.000000Z"
    canceled["payload"]["cancellation"] = {
        "canceled_by": "Test Invitee",
        "reason": "schedule conflict",
        "canceler_type": "invitee",
        "created_at": "2025-07-11T09:30:00.000000Z",
    }
    resp = cal.lambda_handler(api_gw_event(json.dumps(canceled)), None)
    expected_cancel = (
        "calendly/cancellations/dt=2025-07-11/"
        "calendly_cancel_22a0f2d6-1bde-4fc1-95c1-d969df1da21d.json"
    )
    actual_cancel = fake.puts[0]["Key"] if fake.puts else None
    check("cancellation landed", resp["statusCode"] == 200 and len(fake.puts) == 1)
    check("cancellation keyed on cancellation date, own prefix",
          actual_cancel == expected_cancel,
          f"expected {expected_cancel}\n         actual   {actual_cancel}")

    # The failure this guards against: creation and cancellation colliding.
    fake = FakeS3()
    cal.s3 = fake
    cal.lambda_handler(api_gw_event(body), None)
    cal.lambda_handler(api_gw_event(json.dumps(canceled)), None)
    check("cancellation does NOT overwrite the creation",
          len({p["Key"] for p in fake.puts}) == 2,
          f"keys: {[p['Key'] for p in fake.puts]}")

    # Redelivery of the same cancellation stays idempotent.
    cal.lambda_handler(api_gw_event(json.dumps(canceled)), None)
    check("cancellation redelivery overwrites in place",
          len(fake.puts) == 3 and len({p["Key"] for p in fake.puts}) == 2)

    # Payloads omitting the nested cancellation object fall back to updated_at.
    fake = FakeS3()
    cal.s3 = fake
    no_nested = json.loads(json.dumps(canceled))
    del no_nested["payload"]["cancellation"]
    cal.lambda_handler(api_gw_event(json.dumps(no_nested)), None)
    check("falls back to updated_at when cancellation object absent",
          fake.puts and fake.puts[0]["Key"].startswith("calendly/cancellations/dt=2025-07-11/"),
          f"got {fake.puts[0]['Key'] if fake.puts else None}")

    # Unhandled webhook types: 200 (accepted, not persisted) so Calendly stops.
    fake = FakeS3()
    cal.s3 = fake
    other = json.loads(body)
    other["event"] = "invitee_no_show.created"
    resp = cal.lambda_handler(api_gw_event(json.dumps(other)), None)
    check("unhandled event type -> 200 and not persisted",
          resp["statusCode"] == 200 and not fake.puts)

    # Malformed
    resp = cal.lambda_handler(api_gw_event("not json"), None)
    check("unparseable body -> 400", resp["statusCode"] == 400)

    no_uri = json.dumps({"event": "invitee.created", "payload": {"created_at": "2025-07-09T00:00:00Z"}})
    resp = cal.lambda_handler(api_gw_event(no_uri), None)
    check("missing payload.uri -> 400", resp["statusCode"] == 400)

    cal.s3 = FakeS3(fail=True)
    resp = cal.lambda_handler(api_gw_event(body), None)
    check("S3 failure -> 500", resp["statusCode"] == 500)

    # --- signature verification --------------------------------------------
    # Calendly's scheme differs from Close on BOTH axes: the signed string uses
    # a dot separator, and the key is used as raw UTF-8 rather than hex-decoded.
    # These tests pin that difference so the two verifiers cannot be "helpfully"
    # merged into one shared helper later.
    signing_key = "calendly-test-signing-key"
    cal._signing_key_cache = signing_key
    cal.s3 = FakeS3()
    ts = str(int(time.time()))
    good = hmac.new(
        signing_key.encode("utf-8"), f"{ts}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    resp = cal.lambda_handler(
        api_gw_event(body, {"Calendly-Webhook-Signature": f"t={ts},v1={good}"}), None
    )
    check("valid Calendly signature accepted", resp["statusCode"] == 200)

    # The dot matters. Close's separator-less construction must NOT validate.
    no_dot = hmac.new(
        signing_key.encode("utf-8"), f"{ts}{body}".encode(), hashlib.sha256
    ).hexdigest()
    resp = cal.lambda_handler(
        api_gw_event(body, {"Calendly-Webhook-Signature": f"t={ts},v1={no_dot}"}), None
    )
    check("Calendly requires the dot separator -> 401", resp["statusCode"] == 401)

    resp = cal.lambda_handler(
        api_gw_event(body, {"Calendly-Webhook-Signature": f"t={ts},v1=deadbeef"}), None
    )
    check("bad Calendly signature -> 401", resp["statusCode"] == 401)

    resp = cal.lambda_handler(
        api_gw_event(body, {"Calendly-Webhook-Signature": "garbage"}), None
    )
    check("malformed signature header -> 401", resp["statusCode"] == 401)

    old_ts = str(int(time.time()) - 3600)
    stale = hmac.new(
        signing_key.encode("utf-8"), f"{old_ts}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    resp = cal.lambda_handler(
        api_gw_event(body, {"Calendly-Webhook-Signature": f"t={old_ts},v1={stale}"}), None
    )
    check("replayed old Calendly signature -> 401", resp["statusCode"] == 401)

    cal.s3 = FakeS3()
    resp = cal.lambda_handler(
        api_gw_event(body, {"calendly-webhook-signature": f"t={ts},v1={good}"}), None
    )
    check("Calendly header lookup is case-insensitive", resp["statusCode"] == 200)

    # --- fail-closed switch
    cal._signing_key_cache = ""
    cal.s3 = FakeS3()
    resp = cal.lambda_handler(api_gw_event(body), None)
    check("no key + REQUIRE_SIGNATURE off -> accepted", resp["statusCode"] == 200)

    cal.REQUIRE_SIGNATURE = True
    resp = cal.lambda_handler(api_gw_event(body), None)
    check("no key + REQUIRE_SIGNATURE on -> 401", resp["statusCode"] == 401)

    cal.REQUIRE_SIGNATURE = False
    cal._signing_key_cache = ""


if __name__ == "__main__":
    test_crm()
    test_calendly()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
