"""
Reconciliation sweep — drain the awaiting-owner worklist.

Triggered hourly by EventBridge Scheduler (D16). This is a scheduled single
task, not a multi-state workflow, so a scheduler drives it rather than an
orchestrator. Step Functions would be the answer only if this grew branching
stages.

The cadence comes from the downstream freshness need, not a guess: once the
alert has fired, the owner is analytics-only, so landing it within the hour is
ample. The sweep therefore does NOT re-notify — Silver picks the owner up on
its next rebuild (S5).

Per run:
    scan AWAITING rows -> re-read the public bucket
      hit  -> populate the owner cache, drop from the worklist
      miss -> increment retry_count; at the cap, mark EXHAUSTED and escalate

D17: there is no queue here to dead-letter — the sweep scans a table — so the
DLQ *mechanism* does not apply, but the DLQ *pattern* does: cap the retries,
never loop forever, escalate to a human. Implemented as a counter on the row.

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

dynamodb = boto3.client("dynamodb")
ssm = boto3.client("ssm")

OWNER_CACHE_TABLE = os.environ.get("OWNER_CACHE_TABLE", "insightflow-lead-owner")
AWAITING_TABLE = os.environ.get("AWAITING_TABLE", "insightflow-awaiting-owner")

OWNER_BUCKET = os.environ.get("OWNER_BUCKET", "dea-lead-owner")
OWNER_BUCKET_REGION = os.environ.get("OWNER_BUCKET_REGION", "us-east-1")

# Same SecureString the enrich path reads. See crm_enrich for the reasoning.
SLACK_WEBHOOK_PARAM = os.environ.get(
    "SLACK_WEBHOOK_PARAM", "/insightflow/slack/webhook_url"
)
SLACK_WEBHOOK_URL_ENV = os.environ.get("SLACK_WEBHOOK_URL", "")

_slack_url_cache = None


def get_slack_webhook_url():
    """Resolve the Slack webhook URL once per container."""
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
        logger.warning("Could not resolve %s (%s) - not caching", SLACK_WEBHOOK_PARAM, exc)
        return ""

# ~24 hourly sweeps ≈ one day of trying before a human is asked to look.
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "24"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "5"))

# Cap work per run so a large backlog cannot outlive the Lambda timeout. What
# is not reached this hour is reached next hour; the worklist is durable.
MAX_ROWS_PER_RUN = int(os.environ.get("MAX_ROWS_PER_RUN", "500"))


def owner_from_source(lead_id):
    """Unauthenticated GET against the public bucket. 404/403 => not yet assigned."""
    url = (
        f"https://{OWNER_BUCKET}.s3.{OWNER_BUCKET_REGION}.amazonaws.com/"
        f"{urllib.parse.quote(lead_id)}.json"
    )
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return None
        raise


def cache_owner(lead_id, owner_data):
    item = {"lead_id": {"S": lead_id}, "cached_at": {"N": str(int(time.time()))}}
    for field in ("lead_owner", "lead_email", "funnel", "display_name",
                  "status_label", "date_created"):
        value = owner_data.get(field)
        if value:
            item[field] = {"S": str(value)}
    dynamodb.put_item(TableName=OWNER_CACHE_TABLE, Item=item)


def scan_awaiting():
    """
    Read the pending set. It is small by construction — promoted rows are
    deleted — which is exactly why a separate table beats a status flag on the
    main dimension: this scan never walks every lead ever seen.
    """
    rows = []
    kwargs = {
        "TableName": AWAITING_TABLE,
        "FilterExpression": "#st = :awaiting",
        "ExpressionAttributeNames": {"#st": "status"},
        "ExpressionAttributeValues": {":awaiting": {"S": "AWAITING"}},
    }
    while len(rows) < MAX_ROWS_PER_RUN:
        resp = dynamodb.scan(**kwargs)
        rows.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return rows[:MAX_ROWS_PER_RUN]


def promote(lead_id, owner_data):
    """Owner found: populate the cache, then drop the row from the worklist."""
    cache_owner(lead_id, owner_data)
    dynamodb.delete_item(
        TableName=AWAITING_TABLE, Key={"lead_id": {"S": lead_id}}
    )
    logger.info("Promoted lead_id=%s owner=%s", lead_id, owner_data.get("lead_owner"))


def record_miss(lead_id, retry_count):
    """
    Owner still absent. Increment, and give up at the cap (D17).

    The condition guards against two sweeps overlapping — a slow run and the
    next schedule firing would otherwise double-count retries and give up early.
    """
    now = int(time.time())
    next_count = retry_count + 1
    exhausted = next_count >= MAX_RETRIES

    try:
        dynamodb.update_item(
            TableName=AWAITING_TABLE,
            Key={"lead_id": {"S": lead_id}},
            UpdateExpression=(
                "SET retry_count = :next, last_attempt_at = :now, #st = :status"
            ),
            ConditionExpression="retry_count = :current",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":next": {"N": str(next_count)},
                ":current": {"N": str(retry_count)},
                ":now": {"N": str(now)},
                ":status": {"S": "EXHAUSTED" if exhausted else "AWAITING"},
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("lead_id=%s updated concurrently - leaving to the other run",
                        lead_id)
            return False
        raise

    return exhausted


def escalate(exhausted_leads):
    """
    Ask a human to look. Giving up silently would be the actual failure — the
    point of a give-up rule is that someone finds out.
    """
    if not exhausted_leads:
        return

    text = (
        f"*Owner reconciliation exhausted* — {len(exhausted_leads)} lead(s) had no "
        f"owner file after {MAX_RETRIES} hourly sweeps:\n"
        + "\n".join(f"• `{lead_id}`" for lead_id in exhausted_leads[:20])
    )
    if len(exhausted_leads) > 20:
        text += f"\n…and {len(exhausted_leads) - 20} more"

    webhook_url = get_slack_webhook_url()

    if not webhook_url:
        logger.warning("No Slack webhook configured - escalation not sent: %s", text)
        return

    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
        resp.read()


def lambda_handler(event, context):
    rows = scan_awaiting()
    logger.info("Sweep starting: %d awaiting row(s)", len(rows))

    promoted, still_waiting, exhausted, errors = 0, 0, [], 0

    for row in rows:
        lead_id = row["lead_id"]["S"]
        retry_count = int(row.get("retry_count", {}).get("N", "0"))

        try:
            owner_data = owner_from_source(lead_id)
        except Exception:
            # A transient source failure must not consume a retry — that would
            # spend the give-up budget on our own outage rather than on a
            # genuinely missing owner.
            logger.exception("Source read failed for lead_id=%s", lead_id)
            errors += 1
            continue

        if owner_data:
            promote(lead_id, owner_data)
            promoted += 1
        else:
            if record_miss(lead_id, retry_count):
                exhausted.append(lead_id)
            else:
                still_waiting += 1

    escalate(exhausted)

    summary = {
        "scanned": len(rows),
        "promoted": promoted,
        "still_waiting": still_waiting,
        "exhausted": len(exhausted),
        "source_errors": errors,
    }
    logger.info("Sweep complete: %s", json.dumps(summary))
    return summary
