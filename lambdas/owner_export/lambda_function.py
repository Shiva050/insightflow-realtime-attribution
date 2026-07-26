"""
Export the DynamoDB owner state to S3 so Athena can read it.

dim_lead needs lead_owner and lead_email, and dq_lead_coverage needs the
awaiting/exhausted counts. Both live in DynamoDB, which Athena cannot query.
This runs immediately before the Silver build and lands them as NDJSON.

Two tables, two prefixes:

    crm/lead_owner/asof=YYYY-MM-DD/owners.ndjson
    crm/awaiting_owner/asof=YYYY-MM-DD/awaiting.ndjson

Stamped with asof= and kept per build date rather than overwritten. The
coverage table's whole value is its TRAJECTORY — a match rate sliding from 85%
to 60% overnight means the owner sweep broke, and you only see that if
yesterday's snapshot still exists (S7).

This is an export of state we own, not a new source. It stays inside the crm/
prefix because that is where the data originated.

Self-contained by design — console-first deployment, boto3 only, no layers.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")

BRONZE_BUCKET = os.environ.get("BRONZE_BUCKET", "insightflow-bronze")
OWNER_CACHE_TABLE = os.environ.get("OWNER_CACHE_TABLE", "insightflow-lead-owner")
AWAITING_TABLE = os.environ.get("AWAITING_TABLE", "insightflow-awaiting-owner")

OWNER_PREFIX = os.environ.get("OWNER_PREFIX", "crm/lead_owner")
AWAITING_PREFIX = os.environ.get("AWAITING_PREFIX", "crm/awaiting_owner")


def flatten(item):
    """
    DynamoDB's typed attribute form to plain JSON.

    Only S, N, BOOL and NULL appear in these tables; anything else is rendered
    as a string rather than silently dropped, so an unexpected type is visible
    in the data instead of absent from it.
    """
    out = {}
    for key, value in item.items():
        if "S" in value:
            out[key] = value["S"]
        elif "N" in value:
            number = value["N"]
            out[key] = int(number) if "." not in number else float(number)
        elif "BOOL" in value:
            out[key] = value["BOOL"]
        elif "NULL" in value:
            out[key] = None
        else:
            out[key] = json.dumps(value)
    return out


def scan_table(table_name):
    rows = []
    kwargs = {"TableName": table_name}
    while True:
        resp = dynamodb.scan(**kwargs)
        rows.extend(flatten(item) for item in resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return rows
        kwargs["ExclusiveStartKey"] = last_key


def write_ndjson(prefix, filename, rows, asof):
    key = f"{prefix}/asof={asof}/{filename}"
    body = "\n".join(json.dumps(r, separators=(",", ":"), sort_keys=True) for r in rows)
    s3.put_object(
        Bucket=BRONZE_BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
        Metadata={"asof": asof, "row-count": str(len(rows)),
                  "exported-at": str(int(time.time()))},
    )
    return key


def lambda_handler(event, context):
    asof = (event or {}).get("asof") or str(datetime.now(timezone.utc).date())

    owners = scan_table(OWNER_CACHE_TABLE)
    awaiting = scan_table(AWAITING_TABLE)

    owner_key = write_ndjson(OWNER_PREFIX, "owners.ndjson", owners, asof)
    awaiting_key = write_ndjson(AWAITING_PREFIX, "awaiting.ndjson", awaiting, asof)

    summary = {
        "asof": asof,
        "owners_exported": len(owners),
        "awaiting_exported": len(awaiting),
        "exhausted": sum(1 for r in awaiting if r.get("status") == "EXHAUSTED"),
        "keys": [owner_key, awaiting_key],
    }
    logger.info("Owner export complete: %s", json.dumps(summary))
    return summary
