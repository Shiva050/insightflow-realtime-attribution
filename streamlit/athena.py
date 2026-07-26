"""
Athena query helper for the dashboard.

Thin on purpose: the dashboard reads Gold marts that are already shaped for
display, so there is no metric logic here. Anything that looks like a
calculation belongs in a CTAS, not in the presentation layer — otherwise two
consumers of the same mart can disagree.
"""

import os
import time

import boto3
import pandas as pd
import streamlit as st

ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "insightflow")
ATHENA_OUTPUT = os.environ.get(
    "ATHENA_OUTPUT", "s3://insightflow-athena-results/dashboard/")
GOLD_DB = os.environ.get("GOLD_DB", "insightflow_gold")
SILVER_DB = os.environ.get("SILVER_DB", "insightflow_silver")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

POLL_SECONDS = 1.0
QUERY_TIMEOUT_SECONDS = 120


class QueryError(RuntimeError):
    """Raised when Athena rejects a query, carrying the reason it gave."""


@st.cache_resource
def _client():
    return boto3.client("athena", region_name=AWS_REGION)


def _typed(df, schema):
    """Cast Athena's string columns to the types the charts expect."""
    for column, meta in schema.items():
        if column not in df.columns:
            continue
        kind = meta.lower()
        if kind in ("bigint", "integer", "int", "smallint", "tinyint",
                    "double", "float", "real", "decimal"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        elif kind in ("date", "timestamp"):
            df[column] = pd.to_datetime(df[column], errors="coerce")
        elif kind == "boolean":
            df[column] = df[column].map({"true": True, "false": False})
    return df


@st.cache_data(ttl=600, show_spinner=False)
def run_query(sql):
    """
    Execute a query and return a DataFrame.

    Cached for ten minutes: the warehouse rebuilds once a day, so re-querying on
    every widget interaction would spend money to return identical rows.
    """
    client = _client()
    execution = client.start_query_execution(
        QueryString=sql,
        WorkGroup=ATHENA_WORKGROUP,
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
    )
    execution_id = execution["QueryExecutionId"]

    deadline = time.time() + QUERY_TIMEOUT_SECONDS
    while time.time() < deadline:
        status = client.get_query_execution(
            QueryExecutionId=execution_id)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise QueryError(status.get("StateChangeReason", "no reason given"))
        time.sleep(POLL_SECONDS)
    else:
        raise QueryError(f"query still running after {QUERY_TIMEOUT_SECONDS}s")

    rows, columns, token = [], None, None
    while True:
        kwargs = {"QueryExecutionId": execution_id, "MaxResults": 1000}
        if token:
            kwargs["NextToken"] = token
        page = client.get_query_results(**kwargs)

        if columns is None:
            meta = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
            columns = [c["Name"] for c in meta]
            schema = {c["Name"]: c["Type"] for c in meta}
            page_rows = page["ResultSet"]["Rows"][1:]     # first row is headers
        else:
            page_rows = page["ResultSet"]["Rows"]

        for row in page_rows:
            rows.append([field.get("VarCharValue") for field in row["Data"]])

        token = page.get("NextToken")
        if not token:
            break

    return _typed(pd.DataFrame(rows, columns=columns), schema)


def gold(table, where="", order_by="", limit=None):
    sql = f"SELECT * FROM {GOLD_DB}.{table}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {limit}"
    return run_query(sql)


def silver(table, where="", order_by="", limit=None):
    sql = f"SELECT * FROM {SILVER_DB}.{table}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {limit}"
    return run_query(sql)
