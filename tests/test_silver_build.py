"""
Local tests for the Silver build orchestrator, plus a static check of the CTAS
templates themselves.

    python3 tests/test_silver_build.py

Athena cannot be run locally, so the SQL is validated structurally: correct
target table, required placeholders present, no stray ones, and the invariants
that carry design decisions (no play_rate in Silver, spend deduplicated by
as-of). Those checks catch the mistakes that would otherwise surface as a
confident wrong number rather than an error.
"""

import importlib.util
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_DIR = os.path.join(REPO_ROOT, "sql", "silver")

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    line = f"  [{mark}] {name}"
    if detail and not condition:
        line += f"\n         {detail}"
    print(line)


class FakeAthena:
    def __init__(self, fail_on=None):
        self.queries = []
        self.fail_on = fail_on or []
        self._n = 0

    def start_query_execution(self, QueryString, WorkGroup, ResultConfiguration):
        self._n += 1
        self.queries.append(QueryString)
        return {"QueryExecutionId": f"q{self._n}"}

    def get_query_execution(self, QueryExecutionId):
        idx = int(QueryExecutionId[1:]) - 1
        sql = self.queries[idx]
        failed = any(token in sql for token in self.fail_on)
        return {
            "QueryExecution": {
                "Status": {
                    "State": "FAILED" if failed else "SUCCEEDED",
                    "StateChangeReason": "synthetic failure" if failed else "",
                },
                "Statistics": {"TotalExecutionTimeInMillis": 10,
                               "DataScannedInBytes": 100},
            }
        }


class FakeLambda:
    def __init__(self, fail=False):
        self.invocations = []
        self.fail = fail

    def invoke(self, FunctionName, InvocationType, Payload):
        self.invocations.append((FunctionName, json.loads(Payload.decode())))
        body = {"errorMessage": "boom"} if self.fail else {"owners_exported": 3}

        class P:
            def read(self): return json.dumps(body).encode()
        out = {"Payload": P()}
        if self.fail:
            out["FunctionError"] = "Unhandled"
        return out


class FakeGlue:
    def __init__(self, tables=None):
        self.tables = tables or {}

    def get_paginator(self, _op):
        outer = self

        class P:
            def paginate(self, DatabaseName, Expression):
                prefix = Expression.replace("*", "")
                return [{"TableList": [{"Name": n} for n in outer.tables.get(DatabaseName, [])
                                       if n.startswith(prefix)]}]
        return P()


class FakeS3Objects:
    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, _op):
        outer = self

        class P:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k} for k in sorted(outer.objects)
                                      if k.startswith(Prefix)]}]
        return P()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        class B:
            def __init__(self, data): self.data = data
            def read(self): return self.data.encode("utf-8")
        return {"Body": B(self.objects[Key])}


SEED = '{"field_id":"custom.cf_FUNNELID","field_name":"funnel"}\n'


def real_sql_objects():
    """The actual templates from disk, as they would appear in S3."""
    objects = {"seeds/custom_field_map/custom_field_map.ndjson": SEED}
    for filename in sorted(os.listdir(SQL_DIR)):
        if filename.endswith(".sql"):
            with open(os.path.join(SQL_DIR, filename)) as fh:
                objects[f"sql/silver/{filename}"] = fh.read()
    return objects


def load_builder(objects=None, fail_on=None, glue_tables=None):
    spec = importlib.util.spec_from_file_location(
        "silver_build",
        os.path.join(REPO_ROOT, "lambdas", "silver_build", "lambda_function.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["silver_build"] = mod
    spec.loader.exec_module(mod)

    mod.s3 = FakeS3Objects(objects if objects is not None else real_sql_objects())
    mod.athena = FakeAthena(fail_on=fail_on)
    mod.glue = FakeGlue(glue_tables)
    mod.lambda_client = FakeLambda()
    mod.POLL_SECONDS = 0
    return mod


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------
def test_templating():
    print("\nSilver build - templating")
    mod = load_builder()

    rendered = mod.render("a {{x}} b {{y}}", x=1, y="two")
    check("placeholders substituted", rendered == "a 1 b two")

    try:
        mod.render("select {{missing}}", x=1)
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    check("unsubstituted placeholder raises with its name",
          raised and "missing" in raised,
          "an unsubstituted placeholder would reach Athena as literal text")

    stripped = mod.strip_comments("-- header\n-- more\nCREATE TABLE x AS SELECT 1;")
    check("leading comments stripped", stripped.startswith("CREATE TABLE"))
    check("trailing semicolon removed", not stripped.endswith(";"))

    check("funnel field id read from the seed",
          mod.resolve_funnel_field_id() == "custom.cf_FUNNELID")

    mod2 = load_builder(objects={
        "seeds/custom_field_map/custom_field_map.ndjson":
            '{"field_id":"custom.cf_OTHER","field_name":"something_else"}\n'
    })
    try:
        mod2.resolve_funnel_field_id()
        raised = False
    except RuntimeError:
        raised = True
    check("missing funnel mapping raises rather than silently nulling the column",
          raised)


def test_file_discovery():
    print("\nSilver build - SQL discovery")
    mod = load_builder()
    files = mod.load_sql_files()
    tables = [t for t, _, _ in files]

    check("all ten Silver tables discovered", len(files) == 10, f"found {tables}")
    check("dim_lead built before dq_lead_coverage (numeric prefix is the DAG)",
          tables.index("dim_lead") < tables.index("dq_lead_coverage"))
    check("table names derived from filenames",
          set(tables) == {"dim_lead", "dq_lead_coverage", "fct_booking", "fct_invitee",
                          "fct_booking_host", "fct_spend", "dim_media",
                          "fct_media_daily_stats", "dim_visitor", "fct_visitor_event"},
          f"got {sorted(tables)}")

    mod2 = load_builder(objects={"seeds/custom_field_map/custom_field_map.ndjson": SEED})
    try:
        mod2.load_sql_files()
        raised = False
    except RuntimeError:
        raised = True
    check("no SQL files raises rather than reporting an empty success", raised)


# ---------------------------------------------------------------------------
# Build lifecycle
# ---------------------------------------------------------------------------
def test_successful_build():
    print("\nSilver build - happy path")
    mod = load_builder()
    summary = mod.lambda_handler({"build_id": "20260725t060000", "asof": "2026-07-25"}, None)

    check("all tables built", len(summary["built"]) == 10)
    check("all views swapped", len(summary["views_swapped"]) == 10)
    check("status reported as succeeded", summary["status"] == "SUCCEEDED")

    ctas = [q for q in mod.athena.queries if q.startswith("CREATE TABLE")]
    views = [q for q in mod.athena.queries if q.startswith("CREATE OR REPLACE VIEW")]
    check("one CTAS per table", len(ctas) == 10)
    check("one view swap per table", len(views) == 10)

    check("CTAS writes to a build-scoped location",
          all("build_id=20260725t060000" in q for q in ctas))
    check("views point at the versioned build",
          all("__20260725t060000" in q for q in views))

    # Every build must be swapped only after all builds complete, so no view
    # swap may appear before the final CTAS.
    last_ctas = max(i for i, q in enumerate(mod.athena.queries) if q.startswith("CREATE TABLE"))
    first_view = min(i for i, q in enumerate(mod.athena.queries)
                     if q.startswith("CREATE OR REPLACE VIEW"))
    check("no view swapped until every table has built", first_view > last_ctas)

    check("funnel field id templated into dim_lead",
          any("custom.cf_FUNNELID" in q for q in ctas))
    check("no placeholder survived into Athena",
          not any("{{" in q for q in mod.athena.queries))


def test_owner_export_ordering():
    print("\nSilver build - owner export ordering")
    mod = load_builder()
    mod.lambda_handler({"build_id": "b1", "asof": "2026-07-25"}, None)

    check("owner export invoked before the build",
          len(mod.lambda_client.invocations) == 1,
          "dim_lead reads the exported snapshot; a build without it reports "
          "every lead as awaiting an owner")
    fn, payload = mod.lambda_client.invocations[0]
    check("export invoked for the build's asof", payload == {"asof": "2026-07-25"})
    check("export invoked synchronously, not scheduled near the build",
          fn == mod.OWNER_EXPORT_FUNCTION)

    # A failed export must abort the build rather than produce a coverage
    # collapse that looks like the owner sweep broke.
    mod2 = load_builder()
    mod2.lambda_client = FakeLambda(fail=True)
    try:
        mod2.lambda_handler({"build_id": "b2", "asof": "2026-07-25"}, None)
        raised = False
    except RuntimeError:
        raised = True
    check("failed owner export aborts the build", raised)
    check("no tables built after a failed export",
          not any(q.startswith("CREATE TABLE") for q in mod2.athena.queries))

    mod3 = load_builder()
    mod3.lambda_handler({"build_id": "b3", "asof": "2026-07-25",
                         "skip_owner_export": True}, None)
    check("export skippable for a replay against a fixed snapshot",
          not mod3.lambda_client.invocations)


def test_failed_build_leaves_views_alone():
    print("\nSilver build - partial failure")
    mod = load_builder(fail_on=["fct_spend__"])

    try:
        mod.lambda_handler({"build_id": "b2", "asof": "2026-07-25"}, None)
        raised = False
    except RuntimeError:
        raised = True

    check("failed build raises so the scheduler records it", raised)

    views = [q for q in mod.athena.queries if q.startswith("CREATE OR REPLACE VIEW")]
    # A partial swap would leave consumers joining today's bookings to
    # yesterday's spend — worse than stale-but-consistent views.
    check("NO views swapped when any table failed", views == [],
          f"{len(views)} views were swapped despite a failure")

    check("other tables still attempted",
          len([q for q in mod.athena.queries if q.startswith("CREATE TABLE")]) == 10)


def test_pruning():
    print("\nSilver build - build retention")
    old = [f"dim_lead__2026072{i}t000000" for i in range(1, 8)]
    mod = load_builder(glue_tables={"insightflow_silver": old})
    mod.KEEP_BUILDS = 3

    dropped = mod.prune_old_builds("dim_lead", 3)
    check("prunes beyond the retention window", len(dropped) == 4, f"dropped {dropped}")
    check("keeps the newest builds",
          all(d < "dim_lead__20260725t000000" for d in dropped), f"{dropped}")
    check("drops are metadata-only",
          all(q.startswith("DROP TABLE IF EXISTS") for q in mod.athena.queries))


# ---------------------------------------------------------------------------
# Static checks on the templates
# ---------------------------------------------------------------------------
def test_sql_templates():
    print("\nSilver SQL - static checks")
    files = {f: open(os.path.join(SQL_DIR, f)).read()
             for f in sorted(os.listdir(SQL_DIR)) if f.endswith(".sql")}

    bad_target, missing_build, bad_placeholder, no_asof = [], [], [], []
    allowed = {"build_id", "asof", "silver_bucket", "funnel_field_id"}

    for filename, body in files.items():
        table = re.match(r"^\d+_([a-z0-9_]+)\.sql$", filename).group(1)

        if f"CREATE TABLE insightflow_silver.{table}__{{{{build_id}}}}" not in body:
            bad_target.append(filename)
        if f"{table}/build_id={{{{build_id}}}}" not in body:
            missing_build.append(filename)
        for placeholder in set(re.findall(r"\{\{(\w+)\}\}", body)):
            if placeholder not in allowed:
                bad_placeholder.append((filename, placeholder))
        if "build_date" not in body:
            no_asof.append(filename)

    check("every template targets the table named by its filename",
          not bad_target, f"mismatched: {bad_target}")
    check("every template writes to a build-scoped external_location",
          not missing_build, f"missing: {missing_build}")
    check("no unknown placeholders", not bad_placeholder, f"{bad_placeholder}")
    check("every table stamps build_date (Silver is recomputed)",
          not no_asof, f"missing: {no_asof}")

    # Invariants that encode design decisions. Each would otherwise fail as a
    # plausible-looking number rather than an error.
    spend = files["30_fct_spend.sql"]
    check("fct_spend deduplicates the 30-file overlap by as-of date",
          "PARTITION BY spend_date_str, channel" in spend
          and "ORDER BY asof_date DESC" in spend,
          "without this, spend inflates up to 30x and CPB silently lies")

    stats = files["41_fct_media_daily_stats.sql"]
    check("fct_media_daily_stats carries components, never a play_rate column",
          "play_rate" not in stats.split("=====")[-1].replace("-- ", ""),
          "a ratio averaged across days weights a 3-load day like a 3000-load day")
    check("fct_media_daily_stats carries both ratio components",
          "load_count" in stats and "play_count" in stats)

    lead = files["10_dim_lead.sql"]
    check("dim_lead merges on event time with a deterministic tiebreak",
          "event_updated_at_raw" in lead and "event_id DESC" in lead,
          "arrival order is meaningless under retries and parallel consumers")
    check("dim_lead normalises email for the bridge",
          "lead_email_normalized" in lead)

    booking = files["20_fct_booking.sql"]
    check("fct_booking attributes on created_at, not start_time",
          "booking_date_est" in booking and "invitee_created_at_raw" in booking)
    check("fct_booking normalises to EST before truncating to a date",
          "AT TIME ZONE 'America/New_York' AS DATE" in booking,
          "otherwise midnight-boundary bookings land on the wrong day")
    check("fct_booking keeps unmapped channels as 'other', never dropping them",
          "COALESCE(cm.channel, 'other')" in booking)

    host = files["22_fct_booking_host.sql"]
    check("fct_booking_host collapses to one row per meeting before UNNEST",
          "one_row_per_meeting" in host,
          "otherwise a 3-invitee 2-host meeting yields 6 bridge rows, not 2")

    visitor_event = files["51_fct_visitor_event.sql"]
    check("fct_visitor_event materialises is_identified",
          "is_identified" in visitor_event,
          "an empty bridge must be visible in the data, not inferred from no rows")


if __name__ == "__main__":
    test_templating()
    test_file_discovery()
    test_successful_build()
    test_owner_export_ordering()
    test_failed_build_leaves_views_alone()
    test_pruning()
    test_sql_templates()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
