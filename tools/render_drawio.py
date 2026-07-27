#!/usr/bin/env python3
"""
Generate the end-to-end AWS architecture as an editable draw.io file.

This is the third diagram, complementing the two generated SVGs: where those
are tuned for reading in the README, this one uses real AWS iconography and is
meant to be opened at app.diagrams.net, adjusted, and exported as an image.

Everything sits on a fixed grid with one node per cell, so overlap is
impossible by construction rather than by inspection — assert_one_per_cell()
enforces it. Nodes outside the AWS Cloud boundary (the vendor webhooks, the two
third-party S3 buckets, Slack) are placed in the outer columns on purpose, so
the boundary means what it says.

Usage:  python3 tools/render_drawio.py
Writes: assets/insightflow-architecture-aws.drawio
"""

import os
import xml.sax.saxutils as sx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(REPO_ROOT, "assets")

CELL_W, CELL_H = 220, 175
ORIGIN_X, ORIGIN_Y = 120, 150
ICON = 78

# AWS4 category colours, as draw.io's own AWS shape library uses them.
CATEGORY = {
    "compute":   ("#D05C17", "#F78E04"),
    "storage":   ("#277116", "#60A337"),
    "database":  ("#3334B9", "#4D72F3"),
    "integrate": ("#BC1356", "#F34482"),
    "analytics": ("#5A30B5", "#945DF2"),
    "manage":    ("#BC1356", "#F34482"),
    "container": ("#D05C17", "#F78E04"),
}

POINTS = ("[[0,0,0],[0.25,0,0],[0.5,0,0],[0.75,0,0],[1,0,0],[0,1,0],[0.25,1,0],"
          "[0.5,1,0],[0.75,1,0],[1,1,0],[0,0.25,0],[0,0.5,0],[0,0.75,0],"
          "[1,0.25,0],[1,0.5,0],[1,0.75,0]]")


class Svc:
    """An AWS service icon."""
    def __init__(self, key, col, row, label, res, cat):
        self.key, self.col, self.row = key, col, row
        self.label, self.res, self.cat = label, res, cat

    @property
    def x(self):
        return ORIGIN_X + self.col * CELL_W

    @property
    def y(self):
        return ORIGIN_Y + self.row * CELL_H

    def style(self):
        fill, grad = CATEGORY[self.cat]
        return (
            f"sketch=0;points={POINTS};outlineConnect=0;fontColor=#232F3E;"
            f"gradientColor={grad};gradientDirection=north;fillColor={fill};"
            f"strokeColor=#ffffff;dashed=0;verticalLabelPosition=bottom;"
            f"verticalAlign=top;align=center;html=1;fontSize=11;fontStyle=0;"
            f"aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon={self.res};"
        )


class Ext:
    """Something outside the account — a vendor API, a foreign bucket, Slack."""
    def __init__(self, key, col, row, label):
        self.key, self.col, self.row = key, col, row
        self.label = label

    @property
    def x(self):
        return ORIGIN_X + self.col * CELL_W - 16

    @property
    def y(self):
        return ORIGIN_Y + self.row * CELL_H + 14

    def style(self):
        return (
            "rounded=1;whiteSpace=wrap;html=1;fillColor=#F2F4F6;"
            "strokeColor=#6B7A88;fontColor=#232F3E;fontSize=11;"
            "fontStyle=1;arcSize=14;dashed=0;"
        )


def assert_one_per_cell(nodes):
    seen = {}
    for n in nodes:
        cell = (n.col, n.row)
        if cell in seen:
            raise SystemExit(f"cell {cell} claimed by both {seen[cell]} and {n.key}")
        seen[cell] = n.key


# --- the model --------------------------------------------------------------
NODES = [
    # Column 0 — vendors and third-party buckets, deliberately outside the cloud.
    Ext("close",    0, 0, "Close CRM\nwebhook"),
    Ext("calendly", 0, 1, "Calendly\nwebhook"),
    Ext("spendsrc", 0, 2, "dea-data-bucket\npublic S3 · daily spend"),
    Ext("wistiaapi", 0, 3, "Wistia\nStats API"),

    # Ingest edge.
    Svc("apigw",   1, 0, "API Gateway\nHMAC verified", "mxgraph.aws4.api_gateway", "integrate"),
    Svc("schedd",  1, 2, "EventBridge\n06:30 / 07:00 / 07:30 EST", "mxgraph.aws4.eventbridge", "integrate"),

    Svc("crmin",   2, 0, "crm-ingest", "mxgraph.aws4.lambda", "compute"),
    Svc("calin",   2, 1, "calendly-ingest", "mxgraph.aws4.lambda", "compute"),
    Svc("spendin", 2, 2, "spend-ingest\nmanifest diff + hash", "mxgraph.aws4.lambda", "compute"),
    Svc("wistin",  2, 3, "wistia-ingest\nday-windowed", "mxgraph.aws4.lambda", "compute"),
    Svc("build",   2, 4, "warehouse-build\norchestrates CTAS", "mxgraph.aws4.lambda", "compute"),

    # Bronze and shared config.
    Svc("bronze",  3, 1, "insightflow-bronze\nimmutable raw NDJSON", "mxgraph.aws4.s3", "storage"),
    Svc("ssm",     3, 3, "Parameter Store\nSecureString secrets", "mxgraph.aws4.systems_manager", "manage"),
    Svc("athena",  3, 4, "Athena\nCTAS full rebuild", "mxgraph.aws4.athena", "analytics"),

    # Real-time branch.
    Svc("sqs",     4, 0, "SQS delay queue\n10 min", "mxgraph.aws4.sqs", "integrate"),
    Svc("dlq",     4, 1, "SQS DLQ\nmaxReceiveCount 5", "mxgraph.aws4.sqs", "integrate"),
    Svc("sweep",   4, 2, "owner-sweep\nhourly reconciliation", "mxgraph.aws4.lambda", "compute"),
    Svc("cronh",   4, 3, "EventBridge\nhourly", "mxgraph.aws4.eventbridge", "integrate"),
    Svc("glue",    4, 4, "Glue Data Catalog\n3 databases", "mxgraph.aws4.glue", "analytics"),

    Svc("enrich",  5, 0, "crm-enrich\nclaim → post → mark", "mxgraph.aws4.lambda", "compute"),
    Svc("ledger",  5, 1, "event ledger\natomic conditional claim", "mxgraph.aws4.dynamodb", "database"),
    Svc("awaiting", 5, 2, "awaiting-owner\ndurable worklist", "mxgraph.aws4.dynamodb", "database"),
    Svc("silver",  5, 4, "insightflow-silver\n10 conformed tables", "mxgraph.aws4.s3", "storage"),

    Svc("cache",   6, 1, "lead-owner cache\nread-through", "mxgraph.aws4.dynamodb", "database"),
    Svc("logs",    6, 3, "CloudWatch Logs", "mxgraph.aws4.cloudwatch_2", "manage"),
    Svc("gold",    6, 4, "insightflow-gold\n8 metric marts", "mxgraph.aws4.s3", "storage"),
    Svc("dash",    6, 5, "Streamlit on ECS Fargate", "mxgraph.aws4.fargate", "container"),

    # Column 7 — outside the account again.
    Ext("slack",    7, 0, "Slack\nNew Lead Alert"),
    Ext("ownerbkt", 7, 2, "dea-lead-owner\npublic S3 · owner files"),
]

EDGES = [
    ("close", "apigw", "", 0), ("calendly", "apigw", "", 0),
    ("apigw", "crmin", "", 0), ("apigw", "calin", "", 0),
    ("crmin", "bronze", "", 0), ("calin", "bronze", "", 0),
    ("schedd", "spendin", "", 0), ("schedd", "wistin", "", 0),
    ("schedd", "build", "", 0),
    ("spendsrc", "spendin", "", 0), ("wistiaapi", "wistin", "", 0),
    ("spendin", "bronze", "", 0), ("wistin", "bronze", "", 0),

    ("bronze", "sqs", "S3 event", 0),
    ("sqs", "enrich", "", 0),
    ("sqs", "dlq", "poison", 0),
    ("enrich", "ledger", "claim", 0),
    ("enrich", "cache", "owner?", 0),
    ("enrich", "slack", "alert", 0),
    ("enrich", "awaiting", "no owner yet", 0),
    ("cronh", "sweep", "", 0),
    ("sweep", "awaiting", "", 0),
    ("sweep", "ownerbkt", "re-read", 0),
    ("sweep", "slack", "escalation", 0),

    # Secrets are read at cold start, not part of the data flow — dashed.
    ("ssm", "crmin", "signing key", 1),
    ("ssm", "calin", "signing key", 1),
    ("ssm", "enrich", "webhook URL", 1),
    ("ssm", "wistin", "API token", 1),

    ("bronze", "athena", "reads", 0),
    ("build", "athena", "", 0),
    ("athena", "glue", "", 1),
    ("athena", "silver", "CTAS", 0),
    ("athena", "gold", "CTAS", 0),
    ("gold", "dash", "queries", 0),
    ("enrich", "logs", "logs · alarms", 1),
]


def cell(id_, value, style, x, y, w, h):
    return (
        f'        <mxCell id="{id_}" value="{sx.quoteattr(value)[1:-1]}" '
        f'style="{style}" vertex="1" parent="1">\n'
        f'          <mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/>\n'
        f'        </mxCell>'
    )


def edge(i, src, dst, label, dashed):
    style = (
        "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;jettySize=auto;"
        "orthogonalLoop=1;endArrow=blockThin;endFill=1;strokeColor=#545B64;"
        "strokeWidth=1.4;fontSize=10;fontColor=#232F3E;labelBackgroundColor=#FFFFFF;"
        + ("dashed=1;" if dashed else "")
    )
    return (
        f'        <mxCell id="e{i}" value="{sx.quoteattr(label)[1:-1]}" '
        f'style="{style}" edge="1" parent="1" source="{src}" target="{dst}">\n'
        f'          <mxGeometry relative="1" as="geometry"/>\n'
        f'        </mxCell>'
    )


def build():
    assert_one_per_cell(NODES)
    by_key = {n.key: n for n in NODES}
    for s, d, _, _ in EDGES:
        for k in (s, d):
            if k not in by_key:
                raise SystemExit(f"edge references unknown node {k!r}")

    parts = []

    # AWS Cloud boundary, drawn first so it sits behind everything. Columns 1-6
    # only; the vendor systems in columns 0 and 7 stay outside it.
    gx = ORIGIN_X + 1 * CELL_W - 46
    gw = (ORIGIN_X + 6 * CELL_W + ICON + 46) - gx
    gy, gh = ORIGIN_Y - 62, 6 * CELL_H + 40
    parts.append(cell(
        "awscloud", "AWS Cloud — us-east-1 · account 995679261492",
        "points=[[0,0],[0.25,0],[0.5,0],[0.75,0],[1,0],[1,0.25],[1,0.5],[1,0.75],"
        "[1,1],[0.75,1],[0.5,1],[0.25,1],[0,1],[0,0.75],[0,0.5],[0,0.25]];"
        "outlineConnect=0;gradientColor=none;html=1;whiteSpace=wrap;fontSize=12;"
        "fontStyle=1;container=0;pointerEvents=0;collapsible=0;recursiveResize=0;"
        "shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_aws_cloud_alt;"
        "strokeColor=#232F3E;fillColor=none;verticalAlign=top;align=left;"
        "spacingLeft=30;fontColor=#232F3E;dashed=0;",
        gx, gy, gw, gh))

    parts.append(cell(
        "title", "InsightFlow — end-to-end AWS architecture",
        "text;html=1;align=left;verticalAlign=middle;fontSize=22;fontStyle=1;"
        "fontColor=#232F3E;", 120, 40, 900, 32))
    parts.append(cell(
        "subtitle",
        "Bronze / Silver / Gold medallion on Athena CTAS · real-time CRM "
        "alerting in ~10 minutes · dashed edges are secret reads, not data flow",
        "text;html=1;align=left;verticalAlign=middle;fontSize=12;"
        "fontColor=#5A6B7B;", 120, 74, 1100, 20))

    for n in NODES:
        w, h = (ICON, ICON) if isinstance(n, Svc) else (150, 52)
        parts.append(cell(n.key, n.label, n.style(), n.x, n.y, w, h))

    for i, (s, d, label, dashed) in enumerate(EDGES, start=1):
        parts.append(edge(i, s, d, label, dashed))

    page_w = ORIGIN_X + 7 * CELL_W + 200
    page_h = ORIGIN_Y + 5 * CELL_H + 260
    body = "\n".join(parts)
    return (
        '<mxfile host="app.diagrams.net" agent="insightflow/render_drawio.py">\n'
        '  <diagram id="insightflow-aws" name="InsightFlow — AWS end-to-end">\n'
        f'    <mxGraphModel dx="1422" dy="798" grid="0" gridSize="10" guides="1" '
        f'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        f'pageWidth="{page_w}" pageHeight="{page_h}" math="0" shadow="0">\n'
        '      <root>\n'
        '        <mxCell id="0"/>\n'
        '        <mxCell id="1" parent="0"/>\n'
        f'{body}\n'
        '      </root>\n'
        '    </mxGraphModel>\n'
        '  </diagram>\n'
        '</mxfile>\n'
    )


def main():
    os.makedirs(ASSETS, exist_ok=True)
    path = os.path.join(ASSETS, "insightflow-architecture-aws.drawio")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build())
    print(f"wrote {os.path.relpath(path, REPO_ROOT)}")
    print(f"{len(NODES)} nodes, {len(EDGES)} edges; one node per grid cell")


if __name__ == "__main__":
    main()
