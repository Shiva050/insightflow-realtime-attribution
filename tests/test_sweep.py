"""
Local tests for the reconciliation sweep.

    python3 tests/test_sweep.py
"""

import importlib.util
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

from fakes import FakeDynamo  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    line = f"  [{mark}] {name}"
    if detail and not condition:
        line += f"\n         {detail}"
    print(line)


def load_sweep():
    spec = importlib.util.spec_from_file_location(
        "owner_sweep",
        os.path.join(REPO_ROOT, "lambdas", "owner_sweep", "lambda_function.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["owner_sweep"] = mod
    spec.loader.exec_module(mod)
    mod.dynamodb = FakeDynamo()
    mod.escalations = []
    mod.escalate = lambda leads: mod.escalations.extend(leads)
    return mod


OWNER_FILE = {
    "lead_id": "lead_A",
    "lead_owner": "Test Owner",
    "lead_email": "test@example.com",
    "funnel": "DE ACADEMY Direct VSL",
}


def add_awaiting(mod, lead_id, retry_count=0, status="AWAITING"):
    mod.dynamodb.tables.setdefault(mod.AWAITING_TABLE, {})[lead_id] = {
        "lead_id": {"S": lead_id},
        "event_id": {"S": f"ev_{lead_id}"},
        "first_seen_at": {"N": str(int(time.time()) - 3600)},
        "retry_count": {"N": str(retry_count)},
        "status": {"S": status},
    }


def test_promotion():
    print("\nSweep - owner has arrived")
    mod = load_sweep()
    add_awaiting(mod, "lead_A", retry_count=3)
    mod.owner_from_source = lambda lead_id: dict(OWNER_FILE)

    summary = mod.lambda_handler({}, None)

    check("promoted count is 1", summary["promoted"] == 1)
    cache = mod.dynamodb.tables.get(mod.OWNER_CACHE_TABLE, {})
    check("owner written to the cache", "lead_A" in cache)
    check("owner value carried across",
          cache.get("lead_A", {}).get("lead_owner", {}).get("S") == "Test Owner")

    worklist = mod.dynamodb.tables.get(mod.AWAITING_TABLE, {})
    check("row removed from the worklist", "lead_A" not in worklist)
    check("no escalation on success", not mod.escalations)


def test_still_missing():
    print("\nSweep - owner still absent")
    mod = load_sweep()
    add_awaiting(mod, "lead_B", retry_count=5)
    mod.owner_from_source = lambda lead_id: None

    summary = mod.lambda_handler({}, None)

    row = mod.dynamodb.tables[mod.AWAITING_TABLE]["lead_B"]
    check("still_waiting count is 1", summary["still_waiting"] == 1)
    check("retry_count incremented", row["retry_count"]["N"] == "6")
    check("status stays AWAITING", row["status"]["S"] == "AWAITING")
    check("row retained for the next sweep", "lead_B" in mod.dynamodb.tables[mod.AWAITING_TABLE])
    check("no escalation before the cap", not mod.escalations)


def test_give_up_rule():
    print("\nSweep - give-up rule (D17)")
    mod = load_sweep()
    add_awaiting(mod, "lead_C", retry_count=mod.MAX_RETRIES - 1)
    mod.owner_from_source = lambda lead_id: None

    summary = mod.lambda_handler({}, None)

    row = mod.dynamodb.tables[mod.AWAITING_TABLE]["lead_C"]
    check("marked EXHAUSTED at the cap", row["status"]["S"] == "EXHAUSTED")
    check("exhausted count reported", summary["exhausted"] == 1)
    check("escalated to a human", mod.escalations == ["lead_C"])

    # An exhausted row must drop out of the scan, or the sweep polls a ghost
    # owner forever — the exact failure the give-up rule exists to prevent.
    mod.escalations.clear()
    summary2 = mod.lambda_handler({}, None)
    check("EXHAUSTED row not rescanned", summary2["scanned"] == 0)
    check("no repeat escalation", not mod.escalations)


def test_source_error_does_not_consume_budget():
    print("\nSweep - transient source failure")
    mod = load_sweep()
    add_awaiting(mod, "lead_D", retry_count=2)

    def failing_source(lead_id):
        raise TimeoutError("bucket unreachable")

    mod.owner_from_source = failing_source
    summary = mod.lambda_handler({}, None)

    row = mod.dynamodb.tables[mod.AWAITING_TABLE]["lead_D"]
    # Spending the give-up budget on OUR outage would abandon leads whose owner
    # was never actually missing.
    check("retry_count unchanged on source error", row["retry_count"]["N"] == "2")
    check("status unchanged", row["status"]["S"] == "AWAITING")
    check("error counted in the summary", summary["source_errors"] == 1)


def test_mixed_batch_and_concurrency():
    print("\nSweep - mixed batch and overlapping runs")
    mod = load_sweep()
    add_awaiting(mod, "lead_hit", retry_count=1)
    add_awaiting(mod, "lead_miss", retry_count=1)
    add_awaiting(mod, "lead_done", retry_count=0, status="EXHAUSTED")

    mod.owner_from_source = lambda lead_id: dict(OWNER_FILE) if lead_id == "lead_hit" else None
    summary = mod.lambda_handler({}, None)

    check("only AWAITING rows scanned", summary["scanned"] == 2)
    check("hit promoted, miss retained",
          summary["promoted"] == 1 and summary["still_waiting"] == 1)

    # Two overlapping sweeps must not double-count a retry.
    mod2 = load_sweep()
    add_awaiting(mod2, "lead_E", retry_count=4)
    mod2.owner_from_source = lambda lead_id: None
    # Simulate the other run having already incremented this row.
    mod2.dynamodb.tables[mod2.AWAITING_TABLE]["lead_E"]["retry_count"] = {"N": "5"}
    result = mod2.record_miss("lead_E", 4)
    check("stale retry_count is rejected, not double-counted", result is False)
    check("row left at the other run's value",
          mod2.dynamodb.tables[mod2.AWAITING_TABLE]["lead_E"]["retry_count"]["N"] == "5")


if __name__ == "__main__":
    test_promotion()
    test_still_missing()
    test_give_up_rule()
    test_source_error_does_not_consume_budget()
    test_mixed_batch_and_concurrency()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
