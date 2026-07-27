"""
Bronze ingest — Close CRM webhook.

Receives the webhook POST from API Gateway and lands the raw event in S3.
Does nothing else: no enrichment, no owner lookup, no notification. Bronze is
deliberately dumb (D2 — immutable raw is the replay insurance).

Object key: crm/events/dt={date}/crm_event_{event_id}.json

Keyed on event_id, NOT lead_id (D3). A lead emits many events over its
lifecycle; keying on lead_id would silently overwrite the creation event with a
later update, which also breaks the 10-minute delay by making the delayed read
pick up an update as if it were a creation.

NOTE: this deviates from the spec's literal `crm_event_{lead_id}.json`.
The deviation is deliberate and documented for SME review.

The dt= partition is derived from the payload's own date_created, never from
wall-clock (S2). A retry therefore recomputes the identical key and overwrites
itself byte-for-byte instead of creating a second object.

Self-contained by design — console-first deployment, boto3 only, no layers.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
ssm = boto3.client("ssm")

BRONZE_BUCKET = os.environ.get("BRONZE_BUCKET", "insightflow-bronze")
CRM_PREFIX = os.environ.get("CRM_PREFIX", "crm/events")

# Close signs webhooks with HMAC-SHA256 over (timestamp + body), concatenated
# with NO separator. Verified against developer.close.com/api/resources/webhooks
# on 2026-07-26.
#
# The signing key lives in SSM as a SecureString — same treatment as the Wistia
# token. A webhook signing key is a credential; leaving it in a plaintext Lambda
# env var makes it readable by anyone holding lambda:GetFunctionConfiguration.
# The env fallback exists for local tests only.
SIGNING_KEY_PARAM = os.environ.get(
    "CLOSE_SIGNING_KEY_PARAM", "/insightflow/close/signing_key"
)
CLOSE_SIGNING_KEY_ENV = os.environ.get("CLOSE_SIGNING_KEY", "")

# Fail-closed switch. While the SMEs have not yet registered the subscription
# there is no key to verify against, so an unset key is tolerated and logged
# loudly. Set REQUIRE_SIGNATURE=true at registration: from then on a missing key
# is a hard failure rather than a silent open door.
REQUIRE_SIGNATURE = os.environ.get("REQUIRE_SIGNATURE", "").lower() == "true"

# Reject signatures older than this to blunt replay attacks.
SIGNATURE_MAX_AGE_SECONDS = int(os.environ.get("SIGNATURE_MAX_AGE_SECONDS", "300"))

_signing_key_cache = None


def get_signing_key():
    """
    Resolve the Close signing key once per container.

    Returns "" when no key is configured anywhere, which the caller treats
    according to REQUIRE_SIGNATURE.
    """
    global _signing_key_cache
    if _signing_key_cache is not None:
        return _signing_key_cache

    if CLOSE_SIGNING_KEY_ENV:
        logger.warning("Using CLOSE_SIGNING_KEY from the environment - prefer SSM")
        _signing_key_cache = CLOSE_SIGNING_KEY_ENV
        return _signing_key_cache

    try:
        resp = ssm.get_parameter(Name=SIGNING_KEY_PARAM, WithDecryption=True)
        _signing_key_cache = resp["Parameter"]["Value"]
        return _signing_key_cache
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ParameterNotFound":
            raise
        # Definitive: the parameter does not exist. Safe to cache the absence
        # for this container's lifetime.
        logger.warning("SSM parameter %s not found", SIGNING_KEY_PARAM)
        _signing_key_cache = ""
        return _signing_key_cache
    except BotoCoreError as exc:
        # Transient — no credentials, no endpoint, a timeout. Deliberately NOT
        # cached: caching "" here would disable verification for the whole
        # container lifetime because of one blip. Returning "" means the caller
        # falls back to REQUIRE_SIGNATURE, which fails closed once registered.
        logger.warning("Could not resolve %s (%s) - not caching", SIGNING_KEY_PARAM, exc)
        return ""


def _response(status, message, **extra):
    body = {"message": message}
    body.update(extra)
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _get_header(headers, name):
    """API Gateway header casing is not guaranteed; match case-insensitively."""
    if not headers:
        return None
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())


def verify_signature(headers, raw_body):
    """
    Verify the Close webhook signature.

    Returns (ok, reason). With no signing key configured the result depends on
    REQUIRE_SIGNATURE: tolerated (loudly) before the subscription exists, a hard
    failure once it does.
    """
    signing_key = get_signing_key()

    if not signing_key:
        if REQUIRE_SIGNATURE:
            return False, "REQUIRE_SIGNATURE is set but no signing key is configured"
        return True, "verification skipped - no signing key configured"

    sig_hash = _get_header(headers, "Close-Sig-Hash")
    sig_timestamp = _get_header(headers, "Close-Sig-Timestamp")

    if not sig_hash or not sig_timestamp:
        return False, "missing Close-Sig-Hash or Close-Sig-Timestamp header"

    try:
        age = time.time() - int(sig_timestamp)
    except (TypeError, ValueError):
        return False, "Close-Sig-Timestamp is not an integer"

    if abs(age) > SIGNATURE_MAX_AGE_SECONDS:
        return False, f"signature timestamp is {int(age)}s old - outside replay window"

    # Close issues the signature_key as a HEX STRING and signs with its DECODED
    # bytes. Passing the hex text straight to hmac.new() is the trap: a 64-char
    # key would become 64 ASCII bytes instead of the intended 32, producing a
    # valid-looking digest that never matches. Every genuine webhook would 401.
    try:
        key_bytes = bytes.fromhex(signing_key)
    except ValueError:
        return False, "signing key is not valid hex - check the SSM parameter"

    signed_payload = f"{sig_timestamp}{raw_body}".encode("utf-8")
    expected = hmac.new(key_bytes, signed_payload, hashlib.sha256).hexdigest()

    # compare_digest, not == : constant-time, avoids a timing side channel.
    if not hmac.compare_digest(expected, sig_hash):
        return False, "signature mismatch"

    return True, "verified"


def extract_raw_body(event):
    """Pull the verbatim request body out of the API Gateway proxy event."""
    raw_body = event.get("body")
    if raw_body is None:
        raise ValueError("request has no body")
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    return raw_body


def build_object_key(crm_event):
    """
    crm/events/dt={YYYY-MM-DD}/crm_event_{event_id}.json

    dt comes from the event's own date_created so the key is a pure function of
    the payload — a retry regenerates the same key and overwrites itself.
    """
    event_id = crm_event.get("id")
    if not event_id:
        raise ValueError("event.id missing from payload")

    date_created = crm_event.get("date_created") or ""
    dt = date_created[:10]
    if len(dt) != 10:
        raise ValueError(f"event.date_created is not a parseable date: {date_created!r}")

    return f"{CRM_PREFIX}/dt={dt}/crm_event_{event_id}.json"


def lambda_handler(event, context):
    # --- parse -------------------------------------------------------------
    try:
        raw_body = extract_raw_body(event)
        payload = json.loads(raw_body)
    except (ValueError, json.JSONDecodeError) as exc:
        # Malformed and will never parse. 400 so Close stops retrying.
        logger.error("Unparseable request body: %s", exc)
        return _response(400, f"bad request: {exc}")

    # --- authenticate ------------------------------------------------------
    ok, reason = verify_signature(event.get("headers"), raw_body)
    if not ok:
        logger.error("Signature verification failed: %s", reason)
        return _response(401, "signature verification failed")
    if reason.startswith("verification skipped"):
        logger.warning("SIGNATURE NOT VERIFIED: %s", reason)

    # --- locate ------------------------------------------------------------
    crm_event = payload.get("event") or {}
    try:
        key = build_object_key(crm_event)
    except ValueError as exc:
        logger.error("Cannot derive object key: %s", exc)
        return _response(400, f"bad request: {exc}")

    # --- land --------------------------------------------------------------
    # Stored as a single compact line (NDJSON framing). Athena's JSON SerDe
    # reads one record per line and Trino has no multi-line JSON reader, so a
    # pretty-printed body would be unqueryable. Only the framing is normalised:
    # every value and the source key order are preserved exactly, and the
    # signature was verified against the original bytes before we got here.
    try:
        s3.put_object(
            Bucket=BRONZE_BUCKET,
            Key=key,
            Body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            ContentType="application/x-ndjson",
            # Searchable without opening the object; also aids reconciliation.
            Metadata={
                "event-id": str(crm_event.get("id", "")),
                "lead-id": str(crm_event.get("lead_id", "")),
                "action": str(crm_event.get("action", "")),
                "ingested-at": str(int(time.time())),
            },
        )
    except ClientError:
        # Transient. 500 so Close retries and the event is not lost.
        logger.exception("S3 put_object failed for key %s", key)
        return _response(500, "failed to persist event")

    logger.info(
        "Landed event_id=%s lead_id=%s action=%s -> s3://%s/%s",
        crm_event.get("id"),
        crm_event.get("lead_id"),
        crm_event.get("action"),
        BRONZE_BUCKET,
        key,
    )
    return _response(200, "accepted", key=key)
