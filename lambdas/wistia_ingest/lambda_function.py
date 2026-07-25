"""
Bronze ingest — Wistia (scheduled API pull).

Three datasets, three grains:

    media metadata      hashed_id                  2 rows, fixed
    by_date stats       hashed_id + date           bounded by media x dates
    visitor events      event_key                  bounded by audience behaviour

VERIFIED SOURCE CONTRACT (checked live, not inferred — see SOURCE_CONTRACTS.md):

  * Auth is `Authorization: Bearer {token}`. The older documented Basic scheme
    with api:{token} returns 401.

  * /stats/medias/{id}/by_date.json exists, honours INCLUSIVE start_date and
    end_date, and returns one row per date with load_count and play_count.
    It is a FLOW — the API hands us the daily delta directly. No snapshot
    differencing, no LAG, no seed-row baseline, no negative clamping.

  * /stats/events.json accepts media_id, start_date and end_date, all
    inclusive. Unfiltered it returns org-wide events across every media in the
    account, not only the two in scope.

  * per_page caps at 100 (200 and 500 both return 100), and a full page has
    been observed to time out, so requests retry with backoff.

  * ⚠️ PAGINATION IS NEWEST-FIRST. Page 1 ran 05:24 -> 03:06 and page 2
    continued 02:01 -> 01:13.

That last point is why this module has no watermark. Advancing a cursor to the
newest record seen mid-run would set it to the global maximum and make every
unfetched older record permanently invisible — silent loss, no error.

Instead we pull DAY-WINDOWED. Each (media, day) is requested with inclusive
bounds and written to its own object, so:

  * every day is independently re-requestable, and a failed run self-heals on
    the next one by simply asking again (the C7 pattern);
  * there is no cursor state that can be corrupted;
  * sort order stops mattering, because a bounded window is fetched in full
    before anything is written.

Events for a (media, day) are written as ONE object holding every page, rather
than one object per page. Page boundaries are not stable: the feed is
newest-first, so a new event shifts every subsequent page, and page-keyed
objects would duplicate on the next pull. The (media, day) key is stable and
converges.

Self-contained by design — console-first deployment, boto3 only, no layers.
"""

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")
ssm = boto3.client("ssm")

BRONZE_BUCKET = os.environ.get("BRONZE_BUCKET", "insightflow-bronze")
MANIFEST_TABLE = os.environ.get("MANIFEST_TABLE", "insightflow-wistia-manifest")

MEDIA_PREFIX = os.environ.get("MEDIA_PREFIX", "wistia/media")
STATS_PREFIX = os.environ.get("STATS_PREFIX", "wistia/media_stats")
EVENTS_PREFIX = os.environ.get("EVENTS_PREFIX", "wistia/visitor_events")

API_BASE = os.environ.get("WISTIA_API_BASE", "https://api.wistia.com/v1")
MEDIA_IDS = [m.strip() for m in os.environ.get(
    "WISTIA_MEDIA_IDS", "8hunphufxp,9k4tbcdfg0").split(",") if m.strip()]

# The token lives in SSM Parameter Store as a SecureString. The env fallback
# exists for local runs only — this repo is public and a token must never reach
# a committed file or a plaintext Lambda variable.
TOKEN_PARAM = os.environ.get("WISTIA_TOKEN_PARAM", "/insightflow/wistia/api_token")
TOKEN_ENV = os.environ.get("WISTIA_API_TOKEN", "")

# How many days back to re-request each run. Re-pulling is cheap and idempotent,
# and it is what makes a missed run heal itself without a backfill script.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))

PER_PAGE = int(os.environ.get("PER_PAGE", "50"))          # cap is 100; smaller is safer
MAX_PAGES = int(os.environ.get("MAX_PAGES", "200"))       # guard against a runaway loop
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "4"))

_token_cache = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_token():
    """Read the API token from SSM once per container, falling back to env."""
    global _token_cache
    if _token_cache:
        return _token_cache

    if TOKEN_ENV:
        logger.warning("Using WISTIA_API_TOKEN from the environment - prefer SSM")
        _token_cache = TOKEN_ENV
        return _token_cache

    resp = ssm.get_parameter(Name=TOKEN_PARAM, WithDecryption=True)
    _token_cache = resp["Parameter"]["Value"]
    return _token_cache


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def api_get(path, params=None):
    """
    GET with retry and backoff.

    A full page has been seen to time out, and 429/5xx are normal for any API
    under load. Retrying a GET is safe — these reads have no side effects.
    """
    url = f"{API_BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "Authorization": f"Bearer {get_token()}",
        "User-Agent": "InsightFlow Wistia Ingest",
    }

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 4xx other than rate-limiting will not improve on retry.
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            last_error = exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc

        if attempt < MAX_ATTEMPTS:
            backoff = 2 ** attempt
            logger.warning("Attempt %d/%d failed for %s (%s) - retrying in %ds",
                           attempt, MAX_ATTEMPTS, path, last_error, backoff)
            time.sleep(backoff)

    raise RuntimeError(f"GET {path} failed after {MAX_ATTEMPTS} attempts: {last_error}")


def fetch_all_pages(path, params):
    """
    Collect every page of a bounded request.

    Safe despite the newest-first ordering precisely BECAUSE the window is
    bounded: we fetch the whole window before writing anything, so partial
    progress is never committed and there is no cursor to advance incorrectly.
    """
    collected = []
    for page in range(1, MAX_PAGES + 1):
        batch = api_get(path, {**params, "page": page, "per_page": PER_PAGE})
        if not isinstance(batch, list):
            raise ValueError(f"{path} returned {type(batch).__name__}, expected a list")
        collected.extend(batch)
        if len(batch) < PER_PAGE:
            return collected
    logger.warning("Hit MAX_PAGES=%d for %s - window may be truncated", MAX_PAGES, path)
    return collected


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def land(key, payload, metadata=None):
    """
    Write as NDJSON — one record per line.

    A list becomes one line per element; a single object becomes one line. This
    is what Athena's line-oriented JSON SerDe needs, and it is why an events
    object is a stream of records rather than a JSON array.

    Keys are sorted so an unchanged re-pull produces byte-identical output even
    if the API reorders fields, which keeps the overwrite genuinely idempotent.
    """
    rows = payload if isinstance(payload, list) else [payload]
    body = "\n".join(
        json.dumps(row, separators=(",", ":"), sort_keys=True) for row in rows
    ).encode("utf-8")
    s3.put_object(
        Bucket=BRONZE_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/json",
        Metadata={**(metadata or {}), "ingested-at": str(int(time.time()))},
    )
    return hashlib.sha256(body).hexdigest(), len(body)


def record_manifest(pull_key, content_hash, row_count, size):
    """
    Record what we pulled, so a later absence can be told apart from a zero.

    Wistia returns explicit zero rows for quiet days, but an absent object could
    mean either "no activity" or "our pull never ran". The manifest is the
    arbiter, same role it plays for spend (S14).
    """
    dynamodb.put_item(
        TableName=MANIFEST_TABLE,
        Item={
            "pull_key": {"S": pull_key},
            "content_hash": {"S": content_hash},
            "row_count": {"N": str(row_count)},
            "size_bytes": {"N": str(size)},
            "pulled_at": {"N": str(int(time.time()))},
        },
    )


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------
def ingest_media_metadata(media_id, asof):
    """
    Media metadata is a STOCK — current state, not activity.

    Stamped with asof= and kept as a daily snapshot. dim_media is Type 1 so only
    the latest is strictly needed, but snapshots cost two tiny objects a day and
    preserve the option of a Type 2 later. Same instinct as storing cumulative
    and deriving deltas: you can always collapse snapshots, never recover them.
    """
    payload = api_get(f"medias/{media_id}.json")
    key = f"{MEDIA_PREFIX}/asof={asof}/{media_id}.json"
    content_hash, size = land(key, payload, {"hashed-id": media_id})
    record_manifest(f"media#{media_id}#{asof}", content_hash, 1, size)
    return key


def ingest_by_date(media_id, start_date, end_date):
    """
    Daily stats for a media over an inclusive window.

    One request covers the window; we then write ONE OBJECT PER DAY so the
    Bronze grain matches the data's grain (hashed_id + date, W2). Splitting the
    response by date is a demux, not a transformation — no values are altered.

    play_rate is deliberately not derived here. by_date exposes load_count and
    play_count, and a ratio is non-additive: it must be computed once at the
    target grain from summed components, never averaged from daily rates (W7).
    """
    rows = api_get(f"stats/medias/{media_id}/by_date.json",
                   {"start_date": start_date, "end_date": end_date})
    if not isinstance(rows, list):
        raise ValueError(f"by_date returned {type(rows).__name__}, expected a list")

    written = []
    for row in rows:
        day = (row.get("date") or "")[:10]
        if len(day) != 10:
            logger.warning("Skipping by_date row with unusable date: %r", row)
            continue
        key = f"{STATS_PREFIX}/dt={day}/{media_id}.json"
        content_hash, size = land(key, row, {"hashed-id": media_id, "stat-date": day})
        record_manifest(f"stats#{media_id}#{day}", content_hash, 1, size)
        written.append(day)
    return written


def ingest_events(media_id, day):
    """
    Every visitor event for one media on one day.

    Written as a single object per (media, day). Page-keyed objects would be
    wrong: the feed is newest-first, so one new event shifts every subsequent
    page and yesterday's "page 2" is not today's "page 2".

    A day with no activity still writes an object, empty. Its existence is the
    evidence that we asked; absence would be ambiguous.
    """
    events = fetch_all_pages("stats/events.json", {
        "media_id": media_id,
        "start_date": day,
        "end_date": day,
    })

    key = f"{EVENTS_PREFIX}/dt={day}/{media_id}.json"
    content_hash, size = land(key, events, {"hashed-id": media_id, "event-date": day})
    record_manifest(f"events#{media_id}#{day}", content_hash, len(events), size)

    identified = sum(1 for e in events if e.get("email"))
    return len(events), identified


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def resolve_window(event):
    """
    Determine the inclusive date window to pull.

    Defaults to the last LOOKBACK_DAYS. An explicit start_date/end_date in the
    invocation payload drives a backfill, which needs no separate code path
    because every day is independently re-requestable.
    """
    event = event or {}
    if event.get("start_date") and event.get("end_date"):
        return event["start_date"], event["end_date"]

    today = datetime.now(timezone.utc).date()
    return str(today - timedelta(days=LOOKBACK_DAYS - 1)), str(today)


def date_range(start_date, end_date):
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError(f"end_date {end_date} precedes start_date {start_date}")
    return [str(start + timedelta(days=n)) for n in range((end - start).days + 1)]


def lambda_handler(event, context):
    start_date, end_date = resolve_window(event)
    days = date_range(start_date, end_date)
    asof = str(datetime.now(timezone.utc).date())

    logger.info("Wistia pull: media=%s window=%s..%s (%d day(s))",
                MEDIA_IDS, start_date, end_date, len(days))

    summary = {
        "window": {"start": start_date, "end": end_date},
        "media": {},
        "errors": [],
    }

    for media_id in MEDIA_IDS:
        stats = {"stat_days": 0, "event_days": 0, "events": 0, "identified": 0}

        try:
            ingest_media_metadata(media_id, asof)
        except Exception as exc:
            logger.exception("Metadata pull failed for %s", media_id)
            summary["errors"].append({"media": media_id, "stage": "metadata",
                                      "error": str(exc)})

        try:
            stats["stat_days"] = len(ingest_by_date(media_id, start_date, end_date))
        except Exception as exc:
            logger.exception("by_date pull failed for %s", media_id)
            summary["errors"].append({"media": media_id, "stage": "by_date",
                                      "error": str(exc)})

        for day in days:
            try:
                count, identified = ingest_events(media_id, day)
                stats["event_days"] += 1
                stats["events"] += count
                stats["identified"] += identified
            except Exception as exc:
                # One bad day must not abandon the rest of the window. The next
                # run re-requests it, because days are independent.
                logger.exception("Events pull failed for %s on %s", media_id, day)
                summary["errors"].append({"media": media_id, "stage": "events",
                                          "day": day, "error": str(exc)})

        summary["media"][media_id] = stats

    total_events = sum(m["events"] for m in summary["media"].values())
    total_identified = sum(m["identified"] for m in summary["media"].values())
    summary["identification_rate"] = (
        round(total_identified / total_events, 4) if total_events else None
    )

    # Surfaced every run, not computed once at analysis time. If the funnel's
    # email bridge is empty, that is a coverage fact the pipeline should be
    # reporting continuously — a rate that silently sits at zero is exactly the
    # confident-wrong-number failure S7 warns about.
    logger.info("Wistia pull complete: %s", json.dumps(summary))
    if total_events and not total_identified:
        logger.warning(
            "Identification rate is 0%% across %d event(s) - the video->lead "
            "email bridge cannot populate. Report as coverage, never as "
            "'video drove no bookings'.", total_events)

    return summary
