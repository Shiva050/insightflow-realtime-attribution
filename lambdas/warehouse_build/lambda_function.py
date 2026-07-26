"""
Warehouse build orchestrator — Silver, then Gold.

Renders the CTAS templates, runs them through Athena, and atomically repoints
the public views at the new build.

SILVER IS RECOMPUTED, NOT APPENDED
Every run rebuilds every table from Bronze plus current lookup state. That is
what makes late-arriving enrichment work with no backfill script: the
reconciliation sweep lands a late owner in DynamoDB, the owner export lands it
in S3, and the next build picks it up automatically.

The freshness contract this creates is real and worth stating: the same
historical day can show a different figure across builds as enrichment catches
up. That is correct behaviour, not drift, which is why every table carries a
build_date.

THE BUILD-AND-SWAP
Athena CTAS cannot overwrite an existing table, so a "full rebuild" is not one
statement. Each run creates a versioned table at a build_id-suffixed location:

    insightflow_silver.dim_lead__20260725T0600      (the build)
    insightflow_silver.dim_lead                     (a view, what consumers read)

The view is repointed with CREATE OR REPLACE VIEW once every table has built.
That swap is a metadata operation, so consumers never observe a missing or
half-built table, and the previous build stays queryable for comparison. The
obvious alternative — DROP then CTAS — leaves a window where the dashboard reads
nothing, and loses the previous build entirely if the new one fails.

WHY NOT STEP FUNCTIONS
Earlier reasoning assumed a dependency graph worth orchestrating. It is not:
only dq_lead_coverage depends on another table, and at measured volumes each
query runs in seconds. A single Lambda that submits and polls stays far inside
its timeout, and adding a state machine would buy a visual DAG for a
two-node dependency. Revisit if the build grows past a few minutes or the
dependency graph gains real branching.

SQL LIVES IN S3, NOT IN THE DEPLOYMENT PACKAGE
So this stays a single-file console-deployable Lambda. The repo is the source
of truth; `aws s3 sync sql/ s3://.../sql/` publishes it.

Self-contained by design — console-first deployment, boto3 only, no layers.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
athena = boto3.client("athena")
glue = boto3.client("glue")
lambda_client = boto3.client("lambda")

# The owner export is invoked synchronously at the start of the build rather
# than scheduled a few minutes earlier. A time gap between the two is a
# cadence-versus-need race: too small and the export is still running, too large
# and the build reads stale owner state. Invoking it directly makes the ordering
# a guarantee instead of a guess.
OWNER_EXPORT_FUNCTION = os.environ.get("OWNER_EXPORT_FUNCTION", "insightflow-owner-export")

SQL_BUCKET = os.environ.get("SQL_BUCKET", "insightflow-bronze")

SILVER_BUCKET = os.environ.get("SILVER_BUCKET", "insightflow-silver")
SILVER_DB = os.environ.get("SILVER_DB", "insightflow_silver")
GOLD_BUCKET = os.environ.get("GOLD_BUCKET", "insightflow-gold")
GOLD_DB = os.environ.get("GOLD_DB", "insightflow_gold")

# Both layers build in ONE invocation, in this order. Gold reads the Silver
# views, so splitting them across two schedules would reintroduce exactly the
# cadence-versus-need race the owner export avoids: too close and Gold reads a
# half-swapped Silver, too far apart and it reads yesterday's.
LAYERS = [
    {"name": "silver", "prefix": "sql/silver/", "db": SILVER_DB},
    {"name": "gold",   "prefix": "sql/gold/",   "db": GOLD_DB},
]

ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "insightflow")
ATHENA_OUTPUT = os.environ.get(
    "ATHENA_OUTPUT", "s3://insightflow-athena-results/silver/")

SEED_CUSTOM_FIELD_KEY = os.environ.get(
    "SEED_CUSTOM_FIELD_KEY", "seeds/custom_field_map/custom_field_map.ndjson")

# Builds retained before older versioned tables are dropped. More than one so a
# bad build can be compared against its predecessor before being discarded.
KEEP_BUILDS = int(os.environ.get("KEEP_BUILDS", "3"))

POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))
QUERY_TIMEOUT_SECONDS = int(os.environ.get("QUERY_TIMEOUT_SECONDS", "300"))

TABLE_FROM_FILENAME = re.compile(r"^\d+_(?P<table>[a-z0-9_]+)\.sql$")


# ---------------------------------------------------------------------------
# Athena
# ---------------------------------------------------------------------------
def run_query(sql, description):
    """Submit a query and block until it finishes. Raises on failure."""
    response = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=ATHENA_WORKGROUP,
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
    )
    execution_id = response["QueryExecutionId"]
    logger.info("Started %s (%s)", description, execution_id)

    deadline = time.time() + QUERY_TIMEOUT_SECONDS
    while time.time() < deadline:
        result = athena.get_query_execution(QueryExecutionId=execution_id)
        status = result["QueryExecution"]["Status"]
        state = status["State"]

        if state == "SUCCEEDED":
            stats = result["QueryExecution"].get("Statistics", {})
            logger.info("Finished %s in %sms, scanned %s bytes",
                        description,
                        stats.get("TotalExecutionTimeInMillis", "?"),
                        stats.get("DataScannedInBytes", "?"))
            return execution_id

        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", "no reason given")
            raise RuntimeError(f"{description} {state}: {reason}")

        time.sleep(POLL_SECONDS)

    raise TimeoutError(f"{description} still running after {QUERY_TIMEOUT_SECONDS}s")


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------
def load_sql_files(prefix):
    """Read one layer's CTAS templates from S3, ordered by filename."""
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=SQL_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".sql"):
                keys.append(obj["Key"])

    if not keys:
        raise RuntimeError(f"No .sql files under s3://{SQL_BUCKET}/{prefix}")

    files = []
    for key in sorted(keys):
        filename = key.rsplit("/", 1)[-1]
        match = TABLE_FROM_FILENAME.match(filename)
        if not match:
            logger.warning("Skipping unrecognised SQL filename: %s", filename)
            continue
        body = s3.get_object(Bucket=SQL_BUCKET, Key=key)["Body"].read().decode("utf-8")
        files.append((match.group("table"), filename, body))
    return files


def resolve_funnel_field_id():
    """
    Read the opaque Close field ID from the seed and template it in.

    The alternative — resolving it inside SQL — means casting the payload to a
    map and looking the key up at query time, which is fragile against the mixed
    value types in that object. Substituting a literal keeps the map externalised
    as config while leaving the SQL simple.
    """
    try:
        body = s3.get_object(
            Bucket=SQL_BUCKET, Key=SEED_CUSTOM_FIELD_KEY
        )["Body"].read().decode("utf-8")
    except ClientError:
        logger.exception("Cannot read the custom-field seed at %s", SEED_CUSTOM_FIELD_KEY)
        raise

    for line in body.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("field_name") == "funnel":
            return row["field_id"]

    raise RuntimeError(
        f"No mapping with field_name='funnel' in {SEED_CUSTOM_FIELD_KEY} - "
        "dim_lead cannot resolve the funnel column"
    )


def render(template, **params):
    sql = template
    for name, value in params.items():
        sql = sql.replace("{{" + name + "}}", str(value))

    leftover = re.findall(r"\{\{(\w+)\}\}", sql)
    if leftover:
        # An unsubstituted placeholder would reach Athena as literal text and
        # fail with a confusing parse error. Name the actual problem instead.
        raise RuntimeError(f"Unsubstituted placeholders: {sorted(set(leftover))}")
    return sql


def strip_comments(sql):
    """Drop leading comment lines so the statement starts with CREATE."""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip().rstrip(";")


# ---------------------------------------------------------------------------
# Build lifecycle
# ---------------------------------------------------------------------------
def swap_view(table, build_id, database):
    """
    Repoint the public view at the new build. Atomic metadata operation, so
    consumers never see a missing or half-built table.
    """
    sql = (
        f"CREATE OR REPLACE VIEW {database}.{table} AS "
        f"SELECT * FROM {database}.{table}__{build_id}"
    )
    run_query(sql, f"swap view {table}")


def prune_old_builds(table, keep, database):
    """
    Drop versioned tables beyond the retention window.

    Metadata only — the S3 data under the old build_id prefix is left in place,
    so a mistaken prune is recoverable by recreating the table definition. A
    lifecycle rule on the bucket handles the storage.
    """
    dropped = []
    try:
        paginator = glue.get_paginator("get_tables")
        names = []
        for page in paginator.paginate(DatabaseName=database,
                                       Expression=f"{table}__*"):
            names.extend(t["Name"] for t in page.get("TableList", []))
    except ClientError:
        logger.warning("Could not list builds for %s - skipping prune", table)
        return dropped

    # Build IDs are timestamps, so lexical order is chronological.
    for name in sorted(names, reverse=True)[keep:]:
        try:
            run_query(f"DROP TABLE IF EXISTS {database}.{name}", f"prune {name}")
            dropped.append(name)
        except Exception:
            logger.warning("Could not drop %s", name, exc_info=True)
    return dropped


def refresh_owner_export(asof):
    """
    Export current DynamoDB owner state to S3 before the build reads it.

    Synchronous and blocking: dim_lead's enrichment and the coverage table both
    depend on this snapshot, so a build that ran without it would silently
    report every lead as awaiting an owner.
    """
    if not OWNER_EXPORT_FUNCTION:
        logger.warning("OWNER_EXPORT_FUNCTION unset - building against whatever "
                       "owner snapshot already exists for asof=%s", asof)
        return None

    response = lambda_client.invoke(
        FunctionName=OWNER_EXPORT_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps({"asof": asof}).encode("utf-8"),
    )
    payload = json.loads(response["Payload"].read().decode("utf-8"))

    if response.get("FunctionError"):
        raise RuntimeError(f"Owner export failed: {payload}")

    logger.info("Owner export refreshed: %s", json.dumps(payload))
    return payload


def lambda_handler(event, context):
    event = event or {}
    now = datetime.now(timezone.utc)
    build_id = event.get("build_id") or now.strftime("%Y%m%dt%H%M%S")
    asof = event.get("asof") or str(now.date())

    logger.info("Warehouse build %s (asof=%s) starting", build_id, asof)

    owner_export = None
    if not event.get("skip_owner_export"):
        owner_export = refresh_owner_export(asof)

    funnel_field_id = resolve_funnel_field_id()

    built, failed, swapped, prune_summary = {}, [], {}, {}

    for layer in LAYERS:
        name, prefix, database = layer["name"], layer["prefix"], layer["db"]
        built[name], swapped[name] = [], []

        files = load_sql_files(prefix)
        logger.info("Layer %s: %d table(s) to build", name, len(files))

        # Sequential and filename-ordered. The numeric prefix IS the dependency
        # declaration: dq_lead_coverage reads dim_lead's versioned table, and
        # channel_attribution reads cpb_by_channel's.
        for table, filename, template in files:
            sql = strip_comments(render(
                template,
                build_id=build_id,
                asof=asof,
                silver_bucket=SILVER_BUCKET,
                gold_bucket=GOLD_BUCKET,
                funnel_field_id=funnel_field_id,
            ))
            try:
                run_query(sql, f"build {database}.{table}__{build_id}")
                built[name].append(table)
            except Exception as exc:
                logger.exception("Build failed for %s.%s", database, table)
                failed.append({"layer": name, "table": table,
                               "file": filename, "error": str(exc)})

        if failed:
            # Stop at the layer boundary. Building Gold on top of a broken
            # Silver would produce marts that look fine and are wrong.
            logger.error("Layer %s incomplete - not proceeding to later layers", name)
            break

        # Swap this layer's views before the next layer builds, since the next
        # layer reads them.
        for table in built[name]:
            swap_view(table, build_id, database)
            swapped[name].append(table)

    if failed:
        logger.error("Build incomplete - %d table(s) failed, affected views NOT "
                     "swapped. Consumers continue reading the previous build.",
                     len(failed))
    else:
        for layer in LAYERS:
            for table in built[layer["name"]]:
                dropped = prune_old_builds(table, KEEP_BUILDS, layer["db"])
                if dropped:
                    prune_summary[table] = dropped

    summary = {
        "build_id": build_id,
        "asof": asof,
        "owner_export": owner_export,
        "built": built,
        "failed": failed,
        "views_swapped": swapped,
        "pruned": prune_summary,
        "status": "SUCCEEDED" if not failed else "FAILED",
    }
    logger.info("Warehouse build complete: %s", json.dumps(summary))

    if failed:
        # Raise so the scheduler records a failure and alarms can fire. Returning
        # a summary with status FAILED would look like a successful invocation.
        raise RuntimeError(
            f"Warehouse build {build_id} failed for: "
            + ", ".join(f"{f['layer']}.{f['table']}" for f in failed)
        )

    return summary
