"""
Bronze ingest — Calendly invitee webhooks (created + canceled).

Lands one raw object per invitee webhook. A 3-invitee meeting therefore lands 3
objects, all carrying the same scheduled_event URI (C3). The collapse to meeting
grain is a Silver job, not an ingestion job.

Object keys:
    calendly/bookings/dt={date}/calendly_event_{invitee_uuid}.json
    calendly/cancellations/dt={date}/calendly_cancel_{invitee_uuid}.json

Creations and cancellations land under SEPARATE prefixes on purpose. Keyed on
invitee_uuid alone, a cancellation would overwrite its own creation object and
destroy the booking record — the same last-write-wins failure D1/D2 rejects for
CRM. An invitee, like a lead, emits a flow of events; Bronze keeps all of them.

The spec names only invitee.created. We land cancellations too because a
cancelled booking left in the counts inflates every booking metric and flatters
CPB, and a cancellation not captured at ingest cannot be reconstructed later.
Whether Silver nets them out is a separate, reversible decision.

Keyed on the invitee URI alone (C2). The invitee URI is a child path of its
scheduled_event (.../scheduled_events/X/invitees/Y) so Y is already globally
unique; appending the scheduled_event adds no uniqueness AND breaks dedup on
reschedule, where the same invitee acquires a new scheduled_event and would
land a second object — double-counting that person's booking.

The subscription is org-wide, so non-campaign bookings arrive here too. We land
ALL of them (C5). Filtering to the three paid event_types at ingestion would
re-couple raw data to today's CPB requirement and starve the
'Daily Calls Booked by Source' metric, which wants every source. Silver tags,
Gold selects.

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
CALENDLY_PREFIX = os.environ.get("CALENDLY_PREFIX", "calendly/bookings")
CALENDLY_CANCEL_PREFIX = os.environ.get(
    "CALENDLY_CANCEL_PREFIX", "calendly/cancellations"
)

# Calendly signs with HMAC-SHA256 over "{timestamp}.{body}" — note the DOT
# separator, and note that the signing key is used as raw UTF-8. Close's scheme
# differs on both counts (no separator, hex-decoded key), which is exactly why
# these two verifiers stay separate instead of sharing a helper: one shared
# "verify HMAC" function would silently reject every request from one vendor.
#
# The key is one WE choose and hand to Calendly at subscription creation, stored
# as an SSM SecureString. The env fallback exists for local tests only.
SIGNING_KEY_PARAM = os.environ.get(
    "CALENDLY_SIGNING_KEY_PARAM", "/insightflow/calendly/signing_key"
)
CALENDLY_SIGNING_KEY_ENV = os.environ.get("CALENDLY_SIGNING_KEY", "")

# See crm_ingest: tolerated before registration, hard failure after.
REQUIRE_SIGNATURE = os.environ.get("REQUIRE_SIGNATURE", "").lower() == "true"

SIGNATURE_MAX_AGE_SECONDS = int(os.environ.get("SIGNATURE_MAX_AGE_SECONDS", "300"))

_signing_key_cache = None


def get_signing_key():
    """Resolve the Calendly signing key once per container."""
    global _signing_key_cache
    if _signing_key_cache is not None:
        return _signing_key_cache

    if CALENDLY_SIGNING_KEY_ENV:
        logger.warning("Using CALENDLY_SIGNING_KEY from the environment - prefer SSM")
        _signing_key_cache = CALENDLY_SIGNING_KEY_ENV
        return _signing_key_cache

    try:
        resp = ssm.get_parameter(Name=SIGNING_KEY_PARAM, WithDecryption=True)
        _signing_key_cache = resp["Parameter"]["Value"]
        return _signing_key_cache
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ParameterNotFound":
            raise
        logger.warning("SSM parameter %s not found", SIGNING_KEY_PARAM)
        _signing_key_cache = ""
        return _signing_key_cache
    except BotoCoreError as exc:
        # Transient — deliberately not cached. See crm_ingest for the reasoning.
        logger.warning("Could not resolve %s (%s) - not caching", SIGNING_KEY_PARAM, exc)
        return ""

# Events we persist, and where each lands. Anything not listed gets a 200 (so
# Calendly stops redelivering) but is not written.
#
# time_fields is tried in order to derive the dt= partition. Each candidate is a
# field of the payload itself, never wall-clock, so the key stays a pure
# function of the event and a redelivery overwrites in place.
EVENT_ROUTING = {
    "invitee.created": {
        "prefix": lambda: CALENDLY_PREFIX,
        "filename": "calendly_event",
        # The acquisition date (C6) — what CPB attributes on.
        "time_fields": (("created_at",),),
    },
    "invitee.canceled": {
        "prefix": lambda: CALENDLY_CANCEL_PREFIX,
        "filename": "calendly_cancel",
        # When the cancellation happened, not when the booking was made.
        # cancellation.created_at is the precise field; updated_at moves with
        # the cancellation too and covers payloads that omit the nested object.
        "time_fields": (("cancellation", "created_at"), ("updated_at",), ("created_at",)),
    },
}


def _response(status, message, **extra):
    body = {"message": message}
    body.update(extra)
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _get_header(headers, name):
    if not headers:
        return None
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())


def _parse_signature_header(raw):
    """Parse Calendly's 't=<timestamp>,v1=<signature>' header."""
    parts = {}
    for chunk in (raw or "").split(","):
        if "=" in chunk:
            k, _, v = chunk.partition("=")
            parts[k.strip()] = v.strip()
    return parts.get("t"), parts.get("v1")


def verify_signature(headers, raw_body):
    """
    Verify the Calendly webhook signature.

    Returns (ok, reason). With no configured key the result depends on
    REQUIRE_SIGNATURE: tolerated (loudly) before the subscription exists, a hard
    failure once it does.
    """
    signing_key = get_signing_key()

    if not signing_key:
        if REQUIRE_SIGNATURE:
            return False, "REQUIRE_SIGNATURE is set but no signing key is configured"
        return True, "verification skipped - no signing key configured"

    header = _get_header(headers, "Calendly-Webhook-Signature")
    timestamp, signature = _parse_signature_header(header)

    if not timestamp or not signature:
        return False, "missing or malformed Calendly-Webhook-Signature header"

    try:
        age = time.time() - int(timestamp)
    except (TypeError, ValueError):
        return False, "signature timestamp is not an integer"

    if abs(age) > SIGNATURE_MAX_AGE_SECONDS:
        return False, f"signature timestamp is {int(age)}s old - outside replay window"

    # Dot separator, and the key as raw UTF-8 — both differ from Close.
    signed_payload = f"{timestamp}.{raw_body}".encode("utf-8")
    expected = hmac.new(
        signing_key.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"

    return True, "verified"


def extract_raw_body(event):
    raw_body = event.get("body")
    if raw_body is None:
        raise ValueError("request has no body")
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    return raw_body


def invitee_uuid_from_uri(invitee_uri):
    """
    Reduce the invitee URI to its trailing UUID for use in a filename.

    'https://api.calendly.com/scheduled_events/1ac9.../invitees/22a0f2d6-...'
      -> '22a0f2d6-...'

    The full URI cannot be a filename (slashes would fabricate S3 prefixes), and
    the trailing UUID carries the same uniqueness (C2).
    """
    if not invitee_uri:
        raise ValueError("payload.uri missing")
    uuid = invitee_uri.rstrip("/").rsplit("/", 1)[-1]
    if not uuid:
        raise ValueError(f"cannot extract invitee uuid from uri: {invitee_uri!r}")
    return uuid


def _dig(payload, path):
    """Walk a tuple of keys into nested dicts, returning None if any hop misses."""
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def resolve_event_date(payload, time_fields):
    """
    Derive the dt= partition from the first populated payload timestamp.

    Every candidate is a field of the event itself, so the resulting key is a
    pure function of the payload (S2 — event time, never processing time).
    """
    for path in time_fields:
        value = _dig(payload, path)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    tried = " or ".join(".".join(p) for p in time_fields)
    raise ValueError(f"no parseable event date on payload (tried {tried})")


def build_object_key(payload, event_name):
    """
    created:  calendly/bookings/dt={date}/calendly_event_{invitee_uuid}.json
    canceled: calendly/cancellations/dt={date}/calendly_cancel_{invitee_uuid}.json

    Same invitee identity, different prefix and filename stem — so a
    cancellation cannot overwrite the creation it refers to.

    The dt is a UTC date and is a physical layout choice only. CPB attributes on
    created_at normalised to EST, and that normalisation happens in Silver; the
    two need not agree, because Silver is a full rebuild reading every partition.
    """
    route = EVENT_ROUTING[event_name]
    uuid = invitee_uuid_from_uri(payload.get("uri"))
    dt = resolve_event_date(payload, route["time_fields"])
    return f"{route['prefix']()}/dt={dt}/{route['filename']}_{uuid}.json"


def lambda_handler(event, context):
    # --- parse -------------------------------------------------------------
    try:
        raw_body = extract_raw_body(event)
        body = json.loads(raw_body)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("Unparseable request body: %s", exc)
        return _response(400, f"bad request: {exc}")

    # --- authenticate ------------------------------------------------------
    ok, reason = verify_signature(event.get("headers"), raw_body)
    if not ok:
        logger.error("Signature verification failed: %s", reason)
        return _response(401, "signature verification failed")
    if reason.startswith("verification skipped"):
        logger.warning("SIGNATURE NOT VERIFIED: %s", reason)

    # --- route by event type ----------------------------------------------
    event_name = body.get("event")
    if event_name not in EVENT_ROUTING:
        # 200, not 4xx: the delivery was valid, we simply do not persist it.
        # A non-2xx would make Calendly redeliver something we will never want.
        logger.info("Ignoring unhandled event type: %s", event_name)
        return _response(200, "ignored", event=event_name)

    # --- locate ------------------------------------------------------------
    payload = body.get("payload") or {}
    try:
        key = build_object_key(payload, event_name)
    except ValueError as exc:
        logger.error("Cannot derive object key: %s", exc)
        return _response(400, f"bad request: {exc}")

    # --- land --------------------------------------------------------------
    scheduled_event = payload.get("scheduled_event") or {}
    try:
        s3.put_object(
            Bucket=BRONZE_BUCKET,
            Key=key,
            # Single compact line (NDJSON framing) so Athena can read it — its
            # JSON SerDe is line-oriented and Trino cannot read multi-line JSON.
            # Framing only: values and source key order are untouched.
            Body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            ContentType="application/x-ndjson",
            Metadata={
                "webhook-event": str(event_name),
                "invitee-uri": str(payload.get("uri", ""))[:1024],
                "scheduled-event-uri": str(scheduled_event.get("uri", ""))[:1024],
                "event-type": str(scheduled_event.get("event_type", ""))[:1024],
                "ingested-at": str(int(time.time())),
            },
        )
    except ClientError:
        logger.exception("S3 put_object failed for key %s", key)
        return _response(500, "failed to persist event")

    logger.info(
        "Landed %s invitee=%s scheduled_event=%s event_type=%s -> s3://%s/%s",
        event_name,
        payload.get("uri"),
        scheduled_event.get("uri"),
        scheduled_event.get("event_type"),
        BRONZE_BUCKET,
        key,
    )
    return _response(200, "accepted", key=key)
