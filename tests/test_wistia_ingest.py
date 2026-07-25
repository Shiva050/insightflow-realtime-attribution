"""
Local tests for the Wistia puller.

    python3 tests/test_wistia_ingest.py

The behaviour under test is that a bounded, day-windowed pull cannot lose data
the way a cursor over a newest-first feed can. The pagination tests deliberately
serve results newest-first, because that is what the live API does.
"""

import importlib.util
import json
import os
import sys
import types
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def ndjson(body):
    """Parse an NDJSON body back into records. Bronze is line-framed so Athena
    can read it, so tests must read it the same way."""
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class FakeSSM:
    def __init__(self, value="tok"):
        self.value = value
        self.calls = 0

    def get_parameter(self, Name, WithDecryption=False):
        self.calls += 1
        return {"Parameter": {"Value": self.value}}


def load_wistia():
    spec = importlib.util.spec_from_file_location(
        "wistia_ingest",
        os.path.join(REPO_ROOT, "lambdas", "wistia_ingest", "lambda_function.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wistia_ingest"] = mod
    spec.loader.exec_module(mod)

    mod.s3 = FakeS3()
    mod.dynamodb = FakeDynamo()
    mod.ssm = FakeSSM()
    mod._token_cache = None
    mod.TOKEN_ENV = ""
    mod.MEDIA_IDS = ["media_a", "media_b"]
    # Never actually sleep between retries.
    mod.time = types.SimpleNamespace(time=lambda: 1700000000, sleep=lambda s: None)
    return mod


def stub_api(mod, events_by_day=None, by_date_rows=None):
    """Replace api_get with a recorder serving canned responses."""
    mod.api_calls = []
    events_by_day = events_by_day or {}
    by_date_rows = by_date_rows or {}

    def fake_api_get(path, params=None):
        params = params or {}
        mod.api_calls.append((path, dict(params)))

        if path.startswith("medias/"):
            return {"hashed_id": path.split("/")[1].split(".")[0], "name": "Test Media"}

        if "by_date" in path:
            media = path.split("/")[2]
            return by_date_rows.get(media, [])

        if path == "stats/events.json":
            media, day = params.get("media_id"), params.get("start_date")
            page, per_page = params.get("page", 1), params.get("per_page", 50)
            rows = events_by_day.get((media, day), [])
            start = (page - 1) * per_page
            return rows[start:start + per_page]

        raise AssertionError(f"unexpected path {path}")

    mod.api_get = fake_api_get


def event(key, day, email=None, hhmmss="12:00:00"):
    return {
        "event_key": key,
        "visitor_key": f"visitor_{key}",
        "media_id": "media_a",
        "received_at": f"{day}T{hhmmss}.000Z",
        "percent_viewed": 0.5,
        "email": email,
    }


def test_window_resolution():
    print("\nWistia - window resolution")
    mod = load_wistia()

    start, end = mod.resolve_window({"start_date": "2026-06-01", "end_date": "2026-06-03"})
    check("explicit backfill window honoured",
          (start, end) == ("2026-06-01", "2026-06-03"))

    days = mod.date_range("2026-06-01", "2026-06-03")
    check("inclusive range expands correctly",
          days == ["2026-06-01", "2026-06-02", "2026-06-03"], f"got {days}")

    check("single-day window is one day",
          mod.date_range("2026-06-01", "2026-06-01") == ["2026-06-01"])

    mod.LOOKBACK_DAYS = 7
    start, end = mod.resolve_window({})
    check("default window spans LOOKBACK_DAYS", len(mod.date_range(start, end)) == 7)

    try:
        mod.date_range("2026-06-05", "2026-06-01")
        raised = False
    except ValueError:
        raised = True
    check("reversed range raises rather than pulling nothing", raised)


def test_datasets_land_at_the_right_grain():
    print("\nWistia - grains and keys")
    mod = load_wistia()
    stub_api(mod, by_date_rows={
        "media_a": [
            {"date": "2026-07-20", "load_count": 100, "play_count": 5, "hours_watched": 1.0},
            {"date": "2026-07-21", "load_count": 0, "play_count": 0, "hours_watched": 0},
        ],
    })

    mod.ingest_media_metadata("media_a", "2026-07-21")
    keys = [p["Key"] for p in mod.s3.puts]
    check("metadata stamped with asof=",
          keys[0] == "wistia/media/asof=2026-07-21/media_a.json", f"got {keys[0]}")

    mod.s3.puts.clear()
    written = mod.ingest_by_date("media_a", "2026-07-20", "2026-07-21")
    keys = sorted(p["Key"] for p in mod.s3.puts)
    check("by_date split to one object per day",
          keys == ["wistia/media_stats/dt=2026-07-20/media_a.json",
                   "wistia/media_stats/dt=2026-07-21/media_a.json"], f"got {keys}")
    check("both days reported written", written == ["2026-07-20", "2026-07-21"])

    body = ndjson(mod.s3.puts[0]["Body"])[0]
    check("day object holds that day's row, values untouched",
          body["load_count"] == 100 and body["play_count"] == 5)
    check("play_rate NOT derived in Bronze (W7 - divide last)",
          "play_rate" not in body)

    zero_day = ndjson(
        [p for p in mod.s3.puts if "2026-07-21" in p["Key"]][0]["Body"])[0]
    check("zero-activity day still lands as an explicit row",
          zero_day["load_count"] == 0)

    manifest = mod.dynamodb.tables[mod.MANIFEST_TABLE]
    check("manifest keyed per dataset/media/day",
          "stats#media_a#2026-07-20" in manifest and "media#media_a#2026-07-21" in manifest)


def test_pagination_newest_first():
    print("\nWistia - pagination over a newest-first feed")
    mod = load_wistia()
    mod.PER_PAGE = 3

    # 7 events, served newest-first exactly as the live API does.
    day = "2026-07-20"
    rows = [event(f"e{i}", day, hhmmss=f"{23 - i:02d}:00:00") for i in range(7)]
    stub_api(mod, events_by_day={("media_a", day): rows})

    count, identified = mod.ingest_events("media_a", day)
    check("every page collected across the window", count == 7, f"got {count}")

    stored = ndjson(mod.s3.puts[-1]["Body"])
    check("no events dropped between pages",
          {e["event_key"] for e in stored} == {f"e{i}" for i in range(7)})

    pages = [p for path, p in mod.api_calls if path == "stats/events.json"]
    check("paged until a short page ended it", len(pages) == 3,
          f"{len(pages)} page requests")
    check("each request carries the inclusive day bounds",
          all(p["start_date"] == day and p["end_date"] == day for p in pages))
    check("each request is scoped to the media", all(p["media_id"] == "media_a" for p in pages))

    # The whole point of day-windowing: nothing is committed mid-window, so
    # there is no cursor to advance to a global maximum.
    check("one object per media-day, not per page", len(mod.s3.puts) == 1)
    check("multi-record object is line-framed, one event per line",
          mod.s3.puts[-1]["Body"].decode().count("\n") == 6)

    # A day with no activity must still produce evidence that we asked.
    mod2 = load_wistia()
    stub_api(mod2, events_by_day={})
    count2, _ = mod2.ingest_events("media_a", "2026-07-22")
    check("empty day still writes an object, as evidence we asked",
          count2 == 0 and len(mod2.s3.puts) == 1
          and ndjson(mod2.s3.puts[0]["Body"]) == [])


def test_idempotency_and_recovery():
    print("\nWistia - idempotency and recovery")
    mod = load_wistia()
    day = "2026-07-20"
    rows = [event("e1", day), event("e2", day)]
    stub_api(mod, events_by_day={("media_a", day): rows})

    mod.ingest_events("media_a", day)
    first_key = mod.s3.puts[0]["Key"]
    first_body = mod.s3.puts[0]["Body"]

    mod.ingest_events("media_a", day)
    check("re-pull writes the identical key", mod.s3.puts[1]["Key"] == first_key)
    check("re-pull of unchanged data is byte-identical",
          mod.s3.puts[1]["Body"] == first_body)

    # A late-arriving event is picked up by simply asking again — no backfill
    # script, no cursor to rewind.
    rows.append(event("e3", day))
    mod.ingest_events("media_a", day)
    stored = ndjson(mod.s3.puts[-1]["Body"])
    check("re-pull absorbs late-arriving events", len(stored) == 3)
    check("still one object for the day", mod.s3.puts[-1]["Key"] == first_key)


def test_retry_behaviour():
    print("\nWistia - transport retries")
    mod = load_wistia()
    mod.MAX_ATTEMPTS = 3
    attempts = {"n": 0}

    class FakeResp:
        def __init__(self, payload): self.payload = payload
        def read(self): return json.dumps(self.payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def flaky_urlopen(request, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("read timed out")
        return FakeResp([{"ok": True}])

    mod.urllib.request.urlopen = flaky_urlopen
    result = mod.api_get("stats/events.json")
    check("transient timeouts retried then succeed",
          result == [{"ok": True}] and attempts["n"] == 3)

    # A 401 will never improve on retry — fail fast rather than hammer the API.
    def unauthorized(request, timeout=None):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    mod.urllib.request.urlopen = unauthorized
    try:
        mod.api_get("stats/events.json")
        raised = None
    except urllib.error.HTTPError as exc:
        raised = exc.code
    check("401 raises immediately, not retried", raised == 401)

    # Exhausted retries must raise, never return a partial result.
    def always_down(request, timeout=None):
        raise TimeoutError("down")

    mod.urllib.request.urlopen = always_down
    try:
        mod.api_get("stats/events.json")
        raised = False
    except RuntimeError:
        raised = True
    check("exhausted retries raise rather than return empty", raised)


def test_handler_isolation_and_coverage():
    print("\nWistia - handler orchestration")
    mod = load_wistia()
    mod.LOOKBACK_DAYS = 2
    days = ["2026-07-20", "2026-07-21"]
    stub_api(
        mod,
        events_by_day={
            ("media_a", "2026-07-20"): [event("e1", "2026-07-20"),
                                        event("e2", "2026-07-20", email="a@b.com")],
            ("media_a", "2026-07-21"): [event("e3", "2026-07-21")],
        },
        by_date_rows={"media_a": [{"date": d, "load_count": 10, "play_count": 1}
                                  for d in days]},
    )

    summary = mod.lambda_handler({"start_date": days[0], "end_date": days[1]}, None)

    check("no errors on the happy path", summary["errors"] == [])
    check("events counted per media", summary["media"]["media_a"]["events"] == 3)
    check("identification rate surfaced", summary["identification_rate"] == round(1 / 3, 4))

    # One failing day must not abandon the rest of the window.
    mod2 = load_wistia()
    stub_api(mod2, by_date_rows={"media_a": [], "media_b": []})
    original = mod2.api_get

    def failing_on_one_day(path, params=None):
        if path == "stats/events.json" and (params or {}).get("start_date") == "2026-07-20":
            raise RuntimeError("upstream 503")
        return original(path, params)

    mod2.api_get = failing_on_one_day
    summary2 = mod2.lambda_handler({"start_date": "2026-07-20", "end_date": "2026-07-21"}, None)

    check("failed day recorded as an error", len(summary2["errors"]) == 2)
    check("remaining days still pulled",
          summary2["media"]["media_a"]["event_days"] == 1
          and summary2["media"]["media_b"]["event_days"] == 1)

    # Zero events must yield None, not a rate of 0 — "we measured nothing" and
    # "we measured zero" are different claims.
    mod3 = load_wistia()
    stub_api(mod3, by_date_rows={"media_a": [], "media_b": []})
    summary3 = mod3.lambda_handler({"start_date": "2026-07-20", "end_date": "2026-07-20"}, None)
    check("no events yields a null identification rate, not zero",
          summary3["identification_rate"] is None)


def test_token_handling():
    print("\nWistia - token handling")
    mod = load_wistia()
    mod.get_token()
    mod.get_token()
    check("SSM read once per container, then cached", mod.ssm.calls == 1)

    mod2 = load_wistia()
    mod2.TOKEN_ENV = "env-token"
    check("env fallback used when set", mod2.get_token() == "env-token")
    check("SSM not consulted when env token present", mod2.ssm.calls == 0)


if __name__ == "__main__":
    test_window_resolution()
    test_datasets_land_at_the_right_grain()
    test_pagination_newest_first()
    test_idempotency_and_recovery()
    test_retry_behaviour()
    test_handler_isolation_and_coverage()
    test_token_handling()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
