"""
Bronze ingest — Calendly spend (scheduled batch pull).

Runs daily, shortly after the source publishes (~06:00 EST for Day-1). Reads
the published manifest, diffs it against what we have already landed, and pulls
whatever is missing or has changed.

This is NOT "pull today's file" (C7). A naive daily cron has a gap hole: one
failed run leaves a date permanently missing and its CPB silently wrong.
Diffing the manifest against our own ledger converges on "Bronze has every
available file" no matter how many runs failed.

VERIFIED SOURCE CONTRACT (checked against the live bucket, not inferred — W1):

  * file_index.json is {"files": ["spend_data_YYYY-MM-DD.json", ...]}, a
    ROLLING 30-day view. Files older than the window still exist and are
    fetchable; they are simply no longer advertised.

  * Each file contains a 30-DAY TRAILING WINDOW, not a single day.
    spend_data_2026-07-23.json holds 90 rows = 30 dates x 3 channels, covering
    2026-06-24..2026-07-23. The filename date is the AS-OF (publication) date.

  * Today's file does not exist yet; the latest available is Day-1.

Two consequences that shape everything below:

  1. The same (spend_date, channel) appears in up to 30 different files. Bronze
     keeps them all; Silver resolves them last-write-wins ordered by as-of date.
     Bronze does not deduplicate — that would be a Silver concern leaking down.

  2. Missing one day's pull loses nothing, because the next day's file re-covers
     29 of the same 30 dates. Only 30 consecutive missed days lose a date. The
     manifest still matters: it is the arbiter of "absent because zero" versus
     "absent because our pull broke" (S14).

Partitioning is `asof=`, deliberately not `dt=`. Every other source uses `dt=`
for the event's own date; here the filename date is a publication date covering
30 other dates. Naming it `dt=` would invite a later join on the wrong column.

Self-contained by design — console-first deployment, boto3 only, no layers.
"""

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")

BRONZE_BUCKET = os.environ.get("BRONZE_BUCKET", "insightflow-bronze")
SPEND_PREFIX = os.environ.get("SPEND_PREFIX", "calendly/spend")
MANIFEST_TABLE = os.environ.get("MANIFEST_TABLE", "insightflow-spend-manifest")

SOURCE_BASE_URL = os.environ.get(
    "SOURCE_BASE_URL",
    "https://dea-data-bucket.s3.us-east-1.amazonaws.com/calendly_spend_data",
)
INDEX_FILENAME = os.environ.get("INDEX_FILENAME", "file_index.json")

# Re-fetch and re-hash every advertised file each run, not just the missing
# ones. Thirty small GETs a day costs nothing and is the only way to notice a
# file revised in place (C8). Set false to pull only what is absent.
VERIFY_ALL = os.environ.get("VERIFY_ALL", "true").lower() != "false"

HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "15"))

FILENAME_RE = re.compile(r"^spend_data_(\d{4}-\d{2}-\d{2})\.json$")


def http_get(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "InsightFlow Spend Ingest"}
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def fetch_index():
    """
    Read the published manifest.

    Tolerates both {"files": [...]} (the verified shape) and a bare list, so a
    benign source reshuffle does not take the pipeline down. Anything else is
    an error rather than a guess — silently pulling nothing would look like a
    successful run that landed no data.
    """
    raw = http_get(f"{SOURCE_BASE_URL}/{INDEX_FILENAME}")
    payload = json.loads(raw.decode("utf-8"))

    if isinstance(payload, dict) and isinstance(payload.get("files"), list):
        files = payload["files"]
    elif isinstance(payload, list):
        files = payload
    else:
        raise ValueError(f"unrecognised file_index shape: {type(payload).__name__}")

    advertised = {}
    for entry in files:
        name = entry if isinstance(entry, str) else (entry or {}).get("file", "")
        name = name.rsplit("/", 1)[-1]
        match = FILENAME_RE.match(name)
        if match:
            advertised[match.group(1)] = name
        else:
            logger.warning("Ignoring unrecognised index entry: %r", entry)

    if not advertised:
        raise ValueError("file_index contained no recognisable spend files")

    return advertised


def load_manifest():
    """Our own record of what we have landed, keyed on as-of date."""
    known = {}
    kwargs = {"TableName": MANIFEST_TABLE}
    while True:
        resp = dynamodb.scan(**kwargs)
        for item in resp.get("Items", []):
            known[item["asof_date"]["S"]] = item.get("content_hash", {}).get("S", "")
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return known


def summarise(payload):
    """Row/date coverage, recorded so S14 can distinguish zero from missing."""
    if not isinstance(payload, list):
        return {}
    dates = sorted({r.get("date") for r in payload if isinstance(r, dict) and r.get("date")})
    channels = sorted({r.get("channel") for r in payload
                       if isinstance(r, dict) and r.get("channel")})
    return {
        "row_count": len(payload),
        "date_min": dates[0] if dates else "",
        "date_max": dates[-1] if dates else "",
        "distinct_dates": len(dates),
        "channels": ",".join(channels),
    }


def record_manifest(asof_date, content_hash, size, stats):
    item = {
        "asof_date": {"S": asof_date},
        "content_hash": {"S": content_hash},
        "pulled_at": {"N": str(int(time.time()))},
        "size_bytes": {"N": str(size)},
    }
    for key in ("row_count", "distinct_dates"):
        if key in stats:
            item[key] = {"N": str(stats[key])}
    for key in ("date_min", "date_max", "channels"):
        if stats.get(key):
            item[key] = {"S": stats[key]}
    dynamodb.put_item(TableName=MANIFEST_TABLE, Item=item)


def land_file(asof_date, filename, raw):
    """
    Write the file verbatim. Keyed on the as-of date, so a re-pull of unchanged
    bytes is an idempotent overwrite rather than a second object.
    """
    key = f"{SPEND_PREFIX}/asof={asof_date}/{filename}"
    s3.put_object(
        Bucket=BRONZE_BUCKET,
        Key=key,
        Body=raw,
        ContentType="application/json",
        Metadata={"asof-date": asof_date, "ingested-at": str(int(time.time()))},
    )
    return key


def lambda_handler(event, context):
    advertised = fetch_index()
    known = load_manifest()

    logger.info("Index advertises %d file(s); manifest holds %d",
                len(advertised), len(known))

    landed, unchanged, corrected, errors = [], 0, [], []

    for asof_date in sorted(advertised):
        filename = advertised[asof_date]
        is_new = asof_date not in known

        # Skip re-fetching known files only when change detection is disabled.
        if not is_new and not VERIFY_ALL:
            unchanged += 1
            continue

        try:
            raw = http_get(f"{SOURCE_BASE_URL}/{filename}")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            # One unavailable file must not abort the run — the rest still land,
            # and the next run retries this one.
            logger.warning("Fetch failed for %s: %s", filename, exc)
            errors.append({"file": filename, "error": str(exc)})
            continue

        # A hash WE compute, over bytes we hold. Never the source's ETag: it is
        # not a plain MD5 for multipart uploads, and it belongs to a bucket we
        # do not own. last_modified is worse — it moves on touch-without-change.
        content_hash = hashlib.sha256(raw).hexdigest()

        if not is_new and known[asof_date] == content_hash:
            unchanged += 1
            continue

        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("Unparseable spend file %s: %s", filename, exc)
            errors.append({"file": filename, "error": f"invalid json: {exc}"})
            continue

        stats = summarise(payload)

        try:
            key = land_file(asof_date, filename, raw)
        except ClientError as exc:
            logger.exception("S3 write failed for %s", filename)
            errors.append({"file": filename, "error": str(exc)})
            continue

        # Manifest is written AFTER the object lands. The reverse order would
        # record a file we do not have, and the self-healing diff would then
        # skip it forever — the same commit-then-record discipline as D12.
        record_manifest(asof_date, content_hash, len(raw), stats)

        if is_new:
            landed.append(asof_date)
        else:
            # Same as-of date, different bytes: the source revised a published
            # file. Exactly what the content hash exists to catch (C8).
            corrected.append(asof_date)
            logger.warning("Spend file for asof=%s was REVISED at source", asof_date)

        logger.info("Landed asof=%s -> s3://%s/%s (%d rows, %s..%s)",
                    asof_date, BRONZE_BUCKET, key,
                    stats.get("row_count", 0),
                    stats.get("date_min", "?"), stats.get("date_max", "?"))

    # Advertised-but-never-landed is the signal that matters: it means a date is
    # genuinely absent from Bronze, which S14 needs to distinguish a real zero
    # from a broken pull.
    missing = sorted(set(advertised) - set(load_manifest()))

    summary = {
        "advertised": len(advertised),
        "landed": landed,
        "corrected": corrected,
        "unchanged": unchanged,
        "errors": errors,
        "still_missing": missing,
    }
    logger.info("Spend ingest complete: %s", json.dumps(summary))

    if missing:
        logger.warning("Dates advertised but NOT in Bronze: %s", missing)

    return summary
