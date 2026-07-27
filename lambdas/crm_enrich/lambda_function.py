"""
CRM real-time path — enrich and notify.

Triggered by the SQS delay queue, ~10 minutes after the event landed in Bronze.
The delay exists so Close has time to assign a lead owner (D6).

Flow per message:
    claim (atomic)  ->  resolve owner  ->  post to Slack  ->  mark SENT

The message carries only an S3 pointer, never the payload (D7 — claim-check).
Bronze is the durable system of record; the queue just says "go look at this".

Key decisions embodied here:

  D10  Idempotency is an ATOMIC conditional claim, not read-then-write.
       GetItem-then-PutItem is a TOCTOU race: two concurrent invocations both
       read "absent", both proceed, both alert. A conditional PutItem makes
       check-and-claim one operation — exactly one writer wins.

  D11  PENDING is a time-boxed lease sized to worst-case INVOCATION time
       (S3 read + DDB lookup + Slack POST = seconds), not to the 10-minute
       business delay, which is already spent in the queue before we run.
       A crashed claimer's row becomes reclaimable after the lease expires.

  D12  Send-then-mark-SENT. Exactly-once is impossible, so we choose our
       failure: a crash after Slack succeeds causes a duplicate alert; the
       reverse order risks a Slack timeout marking the ledger "notified" and
       losing the alert forever. For a new-lead sales alert a duplicate page is
       annoying, a missed lead is revenue lost.

  D14  Owner enrichment is a read-through cache against a bucket we do not own,
       so we read it once per lead rather than once per event.

  D15  A missing owner is NEVER negative-cached. It goes on a durable
       awaiting-owner worklist, because "the next update will fix it" depends
       on an event that may never arrive.

Self-contained by design — console-first deployment, boto3 only, no layers.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")
ssm = boto3.client("ssm")

LEDGER_TABLE = os.environ.get("LEDGER_TABLE", "insightflow-event-ledger")
OWNER_CACHE_TABLE = os.environ.get("OWNER_CACHE_TABLE", "insightflow-lead-owner")
AWAITING_TABLE = os.environ.get("AWAITING_TABLE", "insightflow-awaiting-owner")

OWNER_BUCKET = os.environ.get("OWNER_BUCKET", "dea-lead-owner")
OWNER_BUCKET_REGION = os.environ.get("OWNER_BUCKET_REGION", "us-east-1")

# A Slack incoming-webhook URL is a credential: anyone holding it can post into
# the channel. It lives in SSM as a SecureString, same as the Wistia token and
# the webhook signing keys. The env var remains as a local-test fallback.
SLACK_WEBHOOK_PARAM = os.environ.get(
    "SLACK_WEBHOOK_PARAM", "/insightflow/slack/webhook_url"
)
SLACK_WEBHOOK_URL_ENV = os.environ.get("SLACK_WEBHOOK_URL", "")

_slack_url_cache = None


def get_slack_webhook_url():
    """
    Resolve the Slack webhook URL once per container.

    Returns "" when nothing is configured, which puts the caller in dry-run:
    the alert is logged rather than posted, so the whole path stays exercisable
    before Slack exists without silently doing nothing.
    """
    global _slack_url_cache
    if _slack_url_cache is not None:
        return _slack_url_cache

    if SLACK_WEBHOOK_URL_ENV:
        logger.warning("Using SLACK_WEBHOOK_URL from the environment - prefer SSM")
        _slack_url_cache = SLACK_WEBHOOK_URL_ENV
        return _slack_url_cache

    try:
        resp = ssm.get_parameter(Name=SLACK_WEBHOOK_PARAM, WithDecryption=True)
        _slack_url_cache = resp["Parameter"]["Value"]
        return _slack_url_cache
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ParameterNotFound":
            raise
        logger.warning("SSM parameter %s not found", SLACK_WEBHOOK_PARAM)
        _slack_url_cache = ""
        return _slack_url_cache
    except BotoCoreError as exc:
        # Transient. Not cached — a blip must not silence alerts for the rest of
        # this container's life.
        logger.warning("Could not resolve %s (%s) - not caching", SLACK_WEBHOOK_PARAM, exc)
        return ""

# D11: the lease measures how long one honest invocation could legitimately
# take, NOT the business delay. Must stay below the SQS visibility timeout, and
# the Lambda timeout must also be <= visibility or SQS redelivers mid-run.
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "120"))

HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "5"))

# Which custom field carries the funnel, for the Silver dimension. The
# notification itself takes funnel from the lookup file, per spec.
FUNNEL_FIELD = os.environ.get("FUNNEL_FIELD", "")


# ---------------------------------------------------------------------------
# Ledger — atomic claim
# ---------------------------------------------------------------------------
def claim_event(event_id):
    """
    Atomically claim an event for processing (D10).

    Succeeds when the event has never been claimed, OR when a previous claim is
    still PENDING but its lease has expired (D11 — the crashed-claimer case).
    Returns True if we own the work, False if another invocation does or the
    event was already SENT.
    """
    now = int(time.time())
    lease_cutoff = now - LEASE_SECONDS

    try:
        dynamodb.put_item(
            TableName=LEDGER_TABLE,
            Item={
                "event_id": {"S": event_id},
                "status": {"S": "PENDING"},
                "claimed_at": {"N": str(now)},
            },
            # One operation: check and claim. No gap for a second worker.
            ConditionExpression=(
                "attribute_not_exists(event_id) OR "
                "(#st = :pending AND claimed_at < :cutoff)"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":pending": {"S": "PENDING"},
                ":cutoff": {"N": str(lease_cutoff)},
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Expected and healthy: someone else holds the claim, or it is done.
            logger.info("event_id=%s already claimed or sent - skipping", event_id)
            return False
        raise


def mark_sent(event_id):
    """Record completion AFTER the notification succeeded (D12)."""
    dynamodb.update_item(
        TableName=LEDGER_TABLE,
        Key={"event_id": {"S": event_id}},
        UpdateExpression="SET #st = :sent, sent_at = :now",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":sent": {"S": "SENT"},
            ":now": {"N": str(int(time.time()))},
        },
    )


# ---------------------------------------------------------------------------
# Owner enrichment — read-through cache (D14)
# ---------------------------------------------------------------------------
def owner_from_cache(lead_id):
    resp = dynamodb.get_item(
        TableName=OWNER_CACHE_TABLE,
        Key={"lead_id": {"S": lead_id}},
        ConsistentRead=True,
    )
    item = resp.get("Item")
    if not item:
        return None
    return {k: v.get("S", "") for k, v in item.items()}


def owner_from_source(lead_id):
    """
    Read the lead-owner file from the public bucket we do not own.

    Unauthenticated GET: the bucket is public, and signing with our credentials
    against a foreign bucket invites avoidable failure modes. A 404 means the
    owner has not been assigned yet — a normal, expected state, not an error.
    """
    url = (
        f"https://{OWNER_BUCKET}.s3.{OWNER_BUCKET_REGION}.amazonaws.com/"
        f"{urllib.parse.quote(lead_id)}.json"
    )
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            # 403 is how S3 reports a missing object when listing is denied.
            logger.info("No owner file yet for lead_id=%s (HTTP %s)", lead_id, exc.code)
            return None
        raise
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("Owner lookup failed for lead_id=%s: %s", lead_id, exc)
        raise


def cache_owner(lead_id, owner_data):
    """
    Populate the local cache so every later read is local (D14).

    Only ever called with real data. A miss is never cached (D15) — negative
    caching turns a transient absence into a permanent one.
    """
    item = {"lead_id": {"S": lead_id}, "cached_at": {"N": str(int(time.time()))}}
    for field in ("lead_owner", "lead_email", "funnel", "display_name",
                  "status_label", "date_created"):
        value = owner_data.get(field)
        if value:
            item[field] = {"S": str(value)}
    dynamodb.put_item(TableName=OWNER_CACHE_TABLE, Item=item)


def resolve_owner(lead_id, action):
    """
    Return owner data for a lead, or None if it does not exist yet.

    Cache first, source on miss — regardless of action. `action` only tells us
    which outcome to EXPECT (a creation will usually miss), never lets us skip
    a lookup: assuming "updated implies already cached" would silently drop the
    owner for any lead whose creation event failed or arrived out of order.
    """
    cached = owner_from_cache(lead_id)
    if cached:
        return cached

    logger.info("Owner cache miss for lead_id=%s (action=%s) - reading source",
                lead_id, action)
    owner_data = owner_from_source(lead_id)
    if owner_data:
        cache_owner(lead_id, owner_data)
    return owner_data


def record_awaiting_owner(lead_id, event_id):
    """
    Put the lead on a durable worklist for the reconciliation sweep (D15).

    Conditional so a redelivery cannot reset first_seen_at or retry_count —
    that would make the give-up rule (D17) unreachable and the sweep would poll
    a ghost owner forever.
    """
    now = int(time.time())
    try:
        dynamodb.put_item(
            TableName=AWAITING_TABLE,
            Item={
                "lead_id": {"S": lead_id},
                "event_id": {"S": event_id},
                "first_seen_at": {"N": str(now)},
                "retry_count": {"N": "0"},
                "status": {"S": "AWAITING"},
            },
            ConditionExpression="attribute_not_exists(lead_id)",
        )
        logger.info("lead_id=%s added to awaiting-owner worklist", lead_id)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Already on the worklist. Leave its counters alone.


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------
def build_alert(crm_event, owner_data):
    """
    Compose the New Lead Alert.

    Field lineage is deliberate and follows the spec: identity fields come from
    the event, and email / owner / funnel come from the lookup.

    Note this differs from dim_lead's lineage in Silver, which prefers the
    source with FULL coverage — every lead has an event, not every lead has an
    owner file. The spec's "fetch funnel from the lookup" describes THIS
    notification payload, not the dimension.
    """
    data = crm_event.get("data") or {}
    owner_data = owner_data or {}

    return {
        "display_name": data.get("display_name") or "",
        "lead_id": crm_event.get("lead_id") or "",
        "date_created": data.get("date_created") or crm_event.get("date_created") or "",
        "status_label": data.get("status_label") or "",
        "lead_email": owner_data.get("lead_email") or "",
        "lead_owner": owner_data.get("lead_owner") or "",
        "funnel": owner_data.get("funnel") or "",
    }


def format_slack_message(alert):
    """Blank rather than absent: a missing owner is information, not an error."""
    lines = [
        "*New Lead Alert*",
        f"*Name:* {alert['display_name'] or '_not provided_'}",
        f"*Lead ID:* `{alert['lead_id']}`",
        f"*Created Date:* {alert['date_created']}",
        f"*Label:* {alert['status_label']}",
        f"*Email:* {alert['lead_email'] or '_pending_'}",
        f"*Lead Owner:* {alert['lead_owner'] or '_pending assignment_'}",
        f"*Funnel:* {alert['funnel'] or '_pending_'}",
    ]
    return {"text": "\n".join(lines)}


def post_to_slack(message):
    """
    POST the alert. Raises on failure so the message returns to the queue and
    the claim expires for a later retry — never swallow a delivery failure.
    """
    webhook_url = get_slack_webhook_url()

    if not webhook_url:
        logger.warning(
            "No Slack webhook configured - alert not sent. Payload: %s",
            json.dumps(message),
        )
        return

    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8")
        if resp.status != 200 or body.strip() != "ok":
            raise RuntimeError(f"Slack rejected the message: {resp.status} {body}")


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------
def s3_pointers_from_message(body):
    """
    Extract (bucket, key) pairs from an S3 event notification.

    Object keys arrive URL-encoded in S3 notifications; unquote or any key with
    an escaped character resolves to a NoSuchKey.
    """
    pointers = []
    for record in body.get("Records", []):
        s3_info = record.get("s3") or {}
        bucket = (s3_info.get("bucket") or {}).get("name")
        key = (s3_info.get("object") or {}).get("key")
        if bucket and key:
            pointers.append((bucket, urllib.parse.unquote_plus(key)))
    return pointers


def process_pointer(bucket, key):
    """Handle one Bronze object end to end."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    payload = json.loads(obj["Body"].read().decode("utf-8"))
    crm_event = payload.get("event") or {}

    event_id = crm_event.get("id")
    lead_id = crm_event.get("lead_id")
    action = crm_event.get("action")

    if not event_id or not lead_id:
        # Unprocessable and will never become processable. Do not retry.
        logger.error("Object %s lacks event id or lead_id - skipping", key)
        return

    # 1. Claim. Losing the race is a normal outcome, not a failure.
    if not claim_event(event_id):
        return

    # 2. Resolve the owner. Absence is expected, not exceptional.
    owner_data = resolve_owner(lead_id, action)
    if not owner_data:
        record_awaiting_owner(lead_id, event_id)

    # 3. Send, then 4. record (D12) — in that order, deliberately.
    alert = build_alert(crm_event, owner_data)
    post_to_slack(format_slack_message(alert))
    mark_sent(event_id)

    logger.info(
        "Notified event_id=%s lead_id=%s owner=%s",
        event_id, lead_id, alert["lead_owner"] or "<pending>",
    )


def lambda_handler(event, context):
    """
    SQS batch handler with partial-batch failure reporting.

    Without batchItemFailures, one poison message forces the whole batch to be
    redelivered, so healthy messages get reprocessed repeatedly. The ledger
    would suppress duplicate alerts, but the wasted work is avoidable.
    """
    failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId")
        try:
            body = json.loads(record["body"])
            for bucket, key in s3_pointers_from_message(body):
                process_pointer(bucket, key)
        except Exception:
            # Failure => leave the claim PENDING. It expires after the lease
            # and the next delivery reclaims it. Retries are capped by
            # maxReceiveCount, after which SQS routes to the DLQ (D13).
            logger.exception("Failed to process message %s", message_id)
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}
