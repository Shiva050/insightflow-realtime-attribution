"""
Local tests for the Calendly spend puller.

    python3 tests/test_spend_ingest.py

The behaviour under test is convergence: however many runs failed, the next run
must leave Bronze holding every advertised file. Gap recovery and in-place
correction detection are the two ways that fails silently.
"""

import hashlib
import importlib.util
import json
import os
import sys
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


def spend_rows(dates, channels=("facebook_paid_ads", "youtube_paid_ads", "tiktok_paid_ads"),
               amount=100.0):
    return [
        {"date": d, "channel": c, "spend": amount}
        for d in dates for c in channels
    ]


def make_source(dates, amount=100.0):
    """
    Build a fake source mirroring the verified contract: each file carries a
    trailing window, so consecutive files overlap heavily.
    """
    index = {"files": [f"spend_data_{d}.json" for d in dates]}
    files = {"file_index.json": json.dumps(index).encode()}
    for i, d in enumerate(dates):
        window = dates[max(0, i - 2): i + 1]  # 3-day window keeps fixtures small
        files[f"spend_data_{d}.json"] = json.dumps(spend_rows(window, amount=amount)).encode()
    return files


def load_puller(source_files):
    spec = importlib.util.spec_from_file_location(
        "spend_ingest",
        os.path.join(REPO_ROOT, "lambdas", "spend_ingest", "lambda_function.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["spend_ingest"] = mod
    spec.loader.exec_module(mod)

    mod.dynamodb = FakeDynamo()
    mod.s3 = FakeS3()
    mod.source_files = source_files
    mod.fetch_log = []

    def fake_get(url):
        name = url.rsplit("/", 1)[-1]
        mod.fetch_log.append(name)
        if name not in mod.source_files:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return mod.source_files[name]

    mod.http_get = fake_get
    return mod


DATES = ["2026-07-21", "2026-07-22", "2026-07-23"]


def test_cold_start():
    print("\nSpend - cold start")
    mod = load_puller(make_source(DATES))
    summary = mod.lambda_handler({}, None)

    check("all advertised files landed", summary["landed"] == DATES,
          f"landed={summary['landed']}")
    check("nothing reported missing", summary["still_missing"] == [])
    check("no errors", summary["errors"] == [])

    keys = [p["Key"] for p in mod.s3.puts]
    check("keyed on asof=, not dt=",
          keys[0] == "calendly/spend/asof=2026-07-21/spend_data_2026-07-21.json",
          f"got {keys[0]}")
    check("one object per advertised file", len(keys) == 3)

    manifest = mod.dynamodb.tables[mod.MANIFEST_TABLE]
    check("manifest records every file", sorted(manifest) == DATES)
    check("manifest carries the date coverage",
          manifest["2026-07-23"].get("date_max", {}).get("S") == "2026-07-23")
    check("manifest carries a content hash",
          len(manifest["2026-07-23"]["content_hash"]["S"]) == 64)


def test_second_run_is_a_noop():
    print("\nSpend - re-run with no source change")
    source = make_source(DATES)
    mod = load_puller(source)
    mod.lambda_handler({}, None)

    mod.s3.puts.clear()
    summary = mod.lambda_handler({}, None)

    check("nothing re-landed", summary["landed"] == [])
    check("nothing marked corrected", summary["corrected"] == [])
    check("all files seen as unchanged", summary["unchanged"] == 3)
    check("no redundant S3 writes", mod.s3.puts == [])


def test_gap_recovery():
    print("\nSpend - gap recovery (C7)")
    source = make_source(DATES)
    mod = load_puller(source)
    mod.lambda_handler({}, None)

    # Simulate a run that failed partway: drop a middle date from our manifest.
    del mod.dynamodb.tables[mod.MANIFEST_TABLE]["2026-07-22"]
    mod.s3.puts.clear()

    summary = mod.lambda_handler({}, None)

    check("the missed date is re-pulled", summary["landed"] == ["2026-07-22"],
          f"landed={summary['landed']}")
    check("only the gap is rewritten", len(mod.s3.puts) == 1)
    check("manifest converges to complete",
          sorted(mod.dynamodb.tables[mod.MANIFEST_TABLE]) == DATES)
    check("nothing left missing", summary["still_missing"] == [])


def test_correction_detection():
    print("\nSpend - in-place correction (C8)")
    source = make_source(DATES)
    mod = load_puller(source)
    mod.lambda_handler({}, None)

    original_hash = mod.dynamodb.tables[mod.MANIFEST_TABLE]["2026-07-22"]["content_hash"]["S"]

    # Same filename, revised numbers — the case a filename-only check misses.
    revised = json.dumps(spend_rows(["2026-07-22"], amount=999.99)).encode()
    mod.source_files["spend_data_2026-07-22.json"] = revised
    mod.s3.puts.clear()

    summary = mod.lambda_handler({}, None)

    check("revision detected as a correction", summary["corrected"] == ["2026-07-22"],
          f"corrected={summary['corrected']}")
    check("not misreported as a new landing", summary["landed"] == [])
    check("revised bytes written to S3", len(mod.s3.puts) == 1)
    new_hash = mod.dynamodb.tables[mod.MANIFEST_TABLE]["2026-07-22"]["content_hash"]["S"]
    check("manifest hash updated", new_hash != original_hash)
    check("hash is ours, computed over the bytes we hold",
          new_hash == hashlib.sha256(revised).hexdigest())

    # With verification disabled, the same revision goes unnoticed — this is
    # what a filename-only check buys you.
    mod2 = load_puller(make_source(DATES))
    mod2.lambda_handler({}, None)
    mod2.VERIFY_ALL = False
    mod2.source_files["spend_data_2026-07-22.json"] = revised
    summary2 = mod2.lambda_handler({}, None)
    check("filename-only checking would MISS the correction",
          summary2["corrected"] == [] and summary2["unchanged"] == 3)


def test_partial_failures():
    print("\nSpend - partial failures")
    source = make_source(DATES)
    del source["spend_data_2026-07-22.json"]  # advertised but 403s
    mod = load_puller(source)

    summary = mod.lambda_handler({}, None)

    check("healthy files still land",
          summary["landed"] == ["2026-07-21", "2026-07-23"],
          f"landed={summary['landed']}")
    check("failure recorded, run not aborted", len(summary["errors"]) == 1)
    check("unavailable date reported as still missing",
          summary["still_missing"] == ["2026-07-22"])

    # Once the source publishes it, the next run heals the gap unprompted.
    mod.source_files["spend_data_2026-07-22.json"] = json.dumps(
        spend_rows(["2026-07-22"])
    ).encode()
    summary2 = mod.lambda_handler({}, None)
    check("next run self-heals the previously failed file",
          summary2["landed"] == ["2026-07-22"] and summary2["still_missing"] == [])

    # Malformed JSON must not land or be recorded as landed.
    mod2 = load_puller(make_source(DATES))
    mod2.source_files["spend_data_2026-07-23.json"] = b"{not json"
    summary3 = mod2.lambda_handler({}, None)
    check("unparseable file not landed",
          "2026-07-23" not in summary3["landed"] and len(summary3["errors"]) == 1)
    check("unparseable file not recorded in the manifest",
          "2026-07-23" not in mod2.dynamodb.tables[mod2.MANIFEST_TABLE])

    # S3 write failure must leave the manifest untouched, or the self-healing
    # diff would skip a file we never actually stored.
    mod3 = load_puller(make_source(DATES))
    mod3.s3 = FakeS3(fail_on="asof=2026-07-22")
    summary4 = mod3.lambda_handler({}, None)
    check("failed S3 write is not recorded in the manifest",
          "2026-07-22" not in mod3.dynamodb.tables.get(mod3.MANIFEST_TABLE, {}))
    check("failed write surfaces as still missing",
          summary4["still_missing"] == ["2026-07-22"])


def test_index_contract():
    print("\nSpend - index contract handling")
    # Verified shape.
    mod = load_puller(make_source(DATES))
    check("dict-with-files shape parsed", len(mod.fetch_index()) == 3)

    # A bare list is tolerated rather than treated as an outage.
    mod2 = load_puller(make_source(DATES))
    mod2.source_files["file_index.json"] = json.dumps(
        [f"spend_data_{d}.json" for d in DATES]
    ).encode()
    check("bare-list shape tolerated", len(mod2.fetch_index()) == 3)

    # Entries that are not spend files are ignored, not fatal.
    mod3 = load_puller(make_source(DATES))
    mod3.source_files["file_index.json"] = json.dumps(
        {"files": ["spend_data_2026-07-21.json", "README.txt", "spend_data_bad.json"]}
    ).encode()
    check("unrecognised entries skipped", list(mod3.fetch_index()) == ["2026-07-21"])

    # An unusable index raises rather than reporting a clean run that did
    # nothing — a silent no-op is the dangerous outcome.
    mod4 = load_puller(make_source(DATES))
    mod4.source_files["file_index.json"] = json.dumps({"unexpected": True}).encode()
    try:
        mod4.fetch_index()
        raised = False
    except ValueError:
        raised = True
    check("unrecognised index shape raises, never silently no-ops", raised)

    mod5 = load_puller(make_source(DATES))
    mod5.source_files["file_index.json"] = json.dumps({"files": []}).encode()
    try:
        mod5.fetch_index()
        raised = False
    except ValueError:
        raised = True
    check("empty index raises", raised)


if __name__ == "__main__":
    test_cold_start()
    test_second_run_is_a_noop()
    test_gap_recovery()
    test_correction_detection()
    test_partial_failures()
    test_index_contract()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
