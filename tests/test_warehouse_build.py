"""
Local tests for the warehouse build orchestrator, plus static checks of the
CTAS templates for both layers.

    python3 tests/test_warehouse_build.py

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
SILVER_DIR = os.path.join(REPO_ROOT, "sql", "silver")
GOLD_DIR = os.path.join(REPO_ROOT, "sql", "gold")

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
    for layer, directory in (("silver", SILVER_DIR), ("gold", GOLD_DIR)):
        for filename in sorted(os.listdir(directory)):
            if filename.endswith(".sql"):
                with open(os.path.join(directory, filename)) as fh:
                    objects[f"sql/{layer}/{filename}"] = fh.read()
    return objects


def load_builder(objects=None, fail_on=None, glue_tables=None):
    spec = importlib.util.spec_from_file_location(
        "warehouse_build",
        os.path.join(REPO_ROOT, "lambdas", "warehouse_build", "lambda_function.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["warehouse_build"] = mod
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
    files = mod.load_sql_files("sql/silver/")
    tables = [t for t, _, _ in files]

    check("all ten Silver tables discovered", len(files) == 10, f"found {tables}")
    check("dim_lead built before dq_lead_coverage (numeric prefix is the DAG)",
          tables.index("dim_lead") < tables.index("dq_lead_coverage"))
    check("table names derived from filenames",
          set(tables) == {"dim_lead", "dq_lead_coverage", "fct_booking", "fct_invitee",
                          "fct_booking_host", "fct_spend", "dim_media",
                          "fct_media_daily_stats", "dim_visitor", "fct_visitor_event"},
          f"got {sorted(tables)}")

    gold = [t for t, _, _ in mod.load_sql_files("sql/gold/")]
    check("all eight Gold marts discovered", len(gold) == 8, f"found {gold}")
    check("cpb built before channel_attribution reads it",
          gold.index("cpb_by_channel") < gold.index("channel_attribution"))

    mod2 = load_builder(objects={"seeds/custom_field_map/custom_field_map.ndjson": SEED})
    try:
        mod2.load_sql_files("sql/silver/")
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

    check("all Silver tables built", len(summary["built"]["silver"]) == 10)
    check("all Gold marts built", len(summary["built"]["gold"]) == 8)
    check("all views swapped",
          len(summary["views_swapped"]["silver"]) == 10
          and len(summary["views_swapped"]["gold"]) == 8)
    check("status reported as succeeded", summary["status"] == "SUCCEEDED")

    ctas = [q for q in mod.athena.queries if q.startswith("CREATE TABLE")]
    views = [q for q in mod.athena.queries if q.startswith("CREATE OR REPLACE VIEW")]
    check("one CTAS per table across both layers", len(ctas) == 18)
    check("one view swap per table", len(views) == 18)

    # Gold reads the Silver views, so every Silver swap must precede the first
    # Gold CTAS.
    last_silver_swap = max(i for i, q in enumerate(mod.athena.queries)
                           if q.startswith("CREATE OR REPLACE VIEW insightflow_silver"))
    first_gold_ctas = min(i for i, q in enumerate(mod.athena.queries)
                          if q.startswith("CREATE TABLE insightflow_gold"))
    check("Silver views swapped before Gold builds against them",
          last_silver_swap < first_gold_ctas)

    check("CTAS writes to a build-scoped location",
          all("build_id=20260725t060000" in q for q in ctas))
    check("views point at the versioned build",
          all("__20260725t060000" in q for q in views))

    # Every build must be swapped only after all builds complete, so no view
    # swap may appear before the final CTAS.
    last_silver_ctas = max(i for i, q in enumerate(mod.athena.queries)
                           if q.startswith("CREATE TABLE insightflow_silver"))
    first_silver_view = min(i for i, q in enumerate(mod.athena.queries)
                            if q.startswith("CREATE OR REPLACE VIEW insightflow_silver"))
    check("no view swapped until its whole layer has built",
          first_silver_view > last_silver_ctas)

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

    check("other tables in the layer still attempted",
          len([q for q in mod.athena.queries if q.startswith("CREATE TABLE")]) == 10)
    check("Gold never built on top of a broken Silver",
          not any(q.startswith("CREATE TABLE insightflow_gold") for q in mod.athena.queries),
          "marts over a broken Silver would look fine and be wrong")


def test_pruning():
    print("\nSilver build - build retention")
    old = [f"dim_lead__2026072{i}t000000" for i in range(1, 8)]
    mod = load_builder(glue_tables={"insightflow_silver": old})
    mod.KEEP_BUILDS = 3

    dropped = mod.prune_old_builds("dim_lead", 3, "insightflow_silver")
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
    files = {f: open(os.path.join(SILVER_DIR, f)).read()
             for f in sorted(os.listdir(SILVER_DIR)) if f.endswith(".sql")}

    bad_target, missing_build, bad_placeholder, no_asof = [], [], [], []
    allowed = {"build_id", "asof", "silver_bucket", "gold_bucket", "funnel_field_id"}

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


def test_gold_templates():
    print("\nGold SQL - static checks")
    files = {f: open(os.path.join(GOLD_DIR, f)).read()
             for f in sorted(os.listdir(GOLD_DIR)) if f.endswith(".sql")}
    allowed = {"build_id", "asof", "silver_bucket", "gold_bucket", "funnel_field_id"}

    bad_target, bad_location, bad_placeholder = [], [], []
    for filename, body in files.items():
        table = re.match(r"^\d+_([a-z0-9_]+)\.sql$", filename).group(1)
        if f"CREATE TABLE insightflow_gold.{table}__{{{{build_id}}}}" not in body:
            bad_target.append(filename)
        if f"{{{{gold_bucket}}}}/{table}/build_id={{{{build_id}}}}" not in body:
            bad_location.append(filename)
        for ph in set(re.findall(r"\{\{(\w+)\}\}", body)):
            if ph not in allowed:
                bad_placeholder.append((filename, ph))

    check("every Gold mart targets the table named by its filename",
          not bad_target, f"{bad_target}")
    check("every Gold mart writes to a build-scoped location in the gold bucket",
          not bad_location, f"{bad_location}")
    check("no unknown placeholders in Gold", not bad_placeholder, f"{bad_placeholder}")

    # --- CPB: the three traps -------------------------------------------------
    cpb = files["10_cpb_by_channel.sql"]
    check("CPB collapses bookings to the spend grain BEFORE joining",
          "bookings_agg" in cpb and "GROUP BY booking_date_est, channel" in cpb,
          "joining fact-to-fact at mismatched grains fans spend out across every "
          "booking row and inflates it silently")
    check("CPB uses a FULL OUTER JOIN so neither side is dropped",
          "FULL OUTER JOIN" in cpb,
          "an inner join hides spend-with-no-bookings and bookings-with-no-spend, "
          "which are exactly the days worth looking at")
    check("CPB is undefined, never zero, when bookings are zero",
          "WHEN j.bookings = 0                            THEN NULL" in cpb
          or "j.bookings = 0" in cpb and "THEN NULL" in cpb,
          "rendering a divide-by-zero as 0 reads as 'free'")
    check("CPB distinguishes genuine zero spend from a broken pull",
          "is_genuine_zero_spend" in cpb and "is_spend_data_missing" in cpb,
          "reporting a failed ingestion as organic makes a channel look "
          "infinitely efficient because the pipeline broke")
    check("CPB carries its components alongside the ratio",
          "j.spend," in cpb and "j.bookings," in cpb)
    check("CPB offers a rolling figure for attribution lag",
          "cpb_7d_rolling" in cpb)

    # --- Funnel: identity is not causality ------------------------------------
    funnel = files["40_video_booking_funnel.sql"]
    check("funnel enforces a temporal guard, not just identity",
          "v.received_at_utc <  bk.created_at_utc" in funnel,
          "a shared key is not a cause; without this, watches AFTER the booking "
          "would credit video for it")
    check("funnel bounds the attribution window",
          "INTERVAL '30' DAY" in funnel,
          "a watch 18 months before a booking probably did not drive it")
    check("funnel collapses the video side before joining",
          "SELECT DISTINCT" in funnel,
          "one booker with 40 sessions would otherwise inflate bookings 40x")
    check("funnel is built backward from bookings",
          "bookings_agg" in funnel and "bookings_with_prior_video" in funnel,
          "the video top is polluted by anonymity, so only the booking side has "
          "a fully-known denominator")
    check("funnel reports its touch rate as a floor",
          "video_touch_rate_floor" in funnel)
    check("funnel carries the identification rate that explains a zero",
          "video_identification_rate" in funnel and "NOT MEASURABLE" in funnel,
          "'cannot measure' and 'measured zero' are different claims")
    check("funnel keeps people and sessions separate",
          "bookings_with_prior_video" in funnel and "prior_video_sessions" in funnel)

    # --- Ratios stay non-additive --------------------------------------------
    media = files["32_media_engagement.sql"]
    check("media play_rate divides summed components, never averages rates",
          "SUM(play_count) OVER" in media and "AVG(" not in media.split("SELECT")[-1],
          "averaging daily rates gave 21% against a true 1.02% on live data")
    check("media mart records that per-channel play_rate is not computable",
          "NOT COMPUTABLE" in media.upper())

    attribution = files["21_channel_attribution.sql"]
    check("channel leaderboard re-aggregates from components, not from daily CPB",
          "SUM(bookings)" in attribution and "SUM(CASE WHEN NOT is_spend_data_missing" in attribution,
          "averaging daily CPB weights a 1-booking day like a 40-booking day")
    check("channel leaderboard excludes broken-spend days from the total",
          "NOT is_spend_data_missing" in attribution)
    check("channel leaderboard carries its spend coverage rate",
          "spend_coverage_rate" in attribution)

    # --- Denominators match their numerators ----------------------------------
    load = files["31_meeting_load_by_employee.sql"]
    check("meeting load divides by tenure weeks, not the reporting window",
          "total_meetings / tenure_weeks_proxy" in load,
          "dividing everyone by the window punishes anyone not present throughout")
    check("meeting load uses tenure weeks, not active weeks",
          "tenure_weeks_proxy" in load and "avg_per_active_week" in load,
          "active-weeks structurally cannot reveal underload - idle weeks drop "
          "out of the denominator")
    check("meeting load labels its proxied denominator in the data",
          "tenure_is_proxied" in load and "tenure_caveat" in load,
          "a wrong-but-available number dressed as the right one is worse than "
          "an honest approximation")
    check("meeting load counts per host off the bridge",
          "fct_booking_host" in load,
          "a co-hosted meeting is a unit of load on each host")

    # --- One name, two metrics ------------------------------------------------
    slots = files["30_booking_time_slots.sql"]
    check("time slots serve both customer-local and business-EST perspectives",
          "customer_local" in slots and "business_est" in slots,
          "one timezone silently serves one consumer and misleads the other")
    # Strip comments: the header explains WHY created_at is not used, so a
    # whole-file grep would match the explanation and fail.
    slots_body = "\n".join(ln for ln in slots.splitlines()
                           if not ln.strip().startswith("--"))
    check("time slots use start_time, not created_at",
          "start_hour_invitee_local" in slots_body and "created_at" not in slots_body,
          "created_at is the acquisition signal and already drives CPB")
    check("time slots exclude, rather than default, rows with no invitee timezone",
          "NOT missing_invitee_timezone" in slots)

    trend = files["20_bookings_trend.sql"]
    check("trend emits a dense date spine so gaps are not drawn as flat lines",
          "date_spine" in trend and "sequence(" in trend)

    daily = files["00_daily_calls_by_source.sql"]
    check("daily calls counts all sources, not just paid",
          "WHERE" not in daily.split("FROM insightflow_silver.fct_booking")[1],
          "Silver tags, Gold selects - this metric wants every source")


if __name__ == "__main__":
    test_templating()
    test_file_discovery()
    test_successful_build()
    test_owner_export_ordering()
    test_failed_build_leaves_views_alone()
    test_pruning()
    test_sql_templates()
    test_gold_templates()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
