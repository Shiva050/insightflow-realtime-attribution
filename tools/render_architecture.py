#!/usr/bin/env python3
"""
Render the InsightFlow architecture diagrams to SVG.

Why a generator instead of a hand-placed drawing: the previous single-canvas
diagram accumulated label collisions that were invisible until the PNG was
exported and eyeballed. Here the layout is data, every node's footprint —
including its text block — is computed, and assert_no_collisions() fails the
build if any two footprints intersect. A diagram that renders is a diagram that
was checked.

SVG rather than PNG so GitHub renders it inline with no export step, and so a
change shows up as a readable diff instead of an opaque binary blob.

Usage:  python3 tools/render_architecture.py
Writes: assets/architecture-overview.svg
        assets/architecture-crm-realtime.svg
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(REPO_ROOT, "assets")

# --- geometry ---------------------------------------------------------------
BOX_W, BOX_H = 190, 62
COL_GAP, ROW_GAP = 96, 58
MARGIN_X, MARGIN_Y = 40, 108

TITLE_SIZE, SUB_SIZE = 13, 10.5
LINE_H = 13.5
CHAR_W = 0.58  # width per character, as a fraction of font size

# AWS service-category colours, plus neutral for anything outside the account.
PALETTE = {
    "compute":   "#ED7100",  # Lambda
    "storage":   "#7AA116",  # S3
    "database":  "#C925D1",  # DynamoDB
    "integrate": "#E7157B",  # API Gateway, SQS, EventBridge
    "analytics": "#8C4FFF",  # Athena, Glue
    "external":  "#5A6B7B",  # Close, Calendly, Wistia, Slack
    "serve":     "#01A88D",  # Streamlit
}

BG = "#12161C"
FG = "#E8EDF2"
MUTED = "#93A1B0"
EDGE = "#7C8B9A"


class Node:
    def __init__(self, key, col, row, title, sub="", kind="compute", span=1):
        self.key, self.col, self.row = key, col, row
        self.title, self.sub, self.kind, self.span = title, sub, kind, span

    @property
    def w(self):
        return BOX_W * self.span + COL_GAP * (self.span - 1)

    @property
    def x(self):
        return MARGIN_X + self.col * (BOX_W + COL_GAP)

    @property
    def y(self):
        return MARGIN_Y + self.row * (BOX_H + ROW_GAP)

    def title_lines(self):
        return [ln for ln in self.title.split("\n") if ln]

    def sub_lines(self):
        return [ln for ln in self.sub.split("\n") if ln]

    def footprint(self):
        """Box plus the text block beneath it — what must not collide."""
        text_h = LINE_H * len(self.sub_lines())
        widest = max(
            [len(ln) * TITLE_SIZE * CHAR_W for ln in self.title_lines()]
            + [len(ln) * SUB_SIZE * CHAR_W for ln in self.sub_lines()]
            or [0]
        )
        w = max(self.w, widest)
        cx = self.x + self.w / 2
        return (cx - w / 2, self.y, cx + w / 2, self.y + BOX_H + 6 + text_h)

    def port(self, side, off=0.0):
        """
        off shifts a top/bottom port sideways. Subtitles are centred under the
        box, so a vertical arrow leaving dead centre draws straight through its
        own caption. Offsetting toward the edge keeps the line clear of text.
        """
        cx, cy = self.x + self.w / 2, self.y + BOX_H / 2
        return {
            "l": (self.x, cy), "r": (self.x + self.w, cy),
            "t": (cx + off, self.y), "b": (cx + off, self.y + BOX_H),
        }[side]


def content_bounds(nodes):
    """Union of every node footprint — drives band and canvas sizing."""
    boxes = [n.footprint() for n in nodes.values()]
    return (
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    )


def assert_below(nodes, y, what):
    """Guard decorative blocks against the node grid — the overlap the
    node-to-node check cannot see."""
    bottom = content_bounds(nodes)[3]
    if y < bottom:
        raise SystemExit(f"{what} at y={y} would overlap content ending at y={bottom:.0f}")


def assert_no_collisions(nodes, pad=10):
    """The whole point of generating rather than hand-placing."""
    problems = []
    items = list(nodes.values())
    for i, a in enumerate(items):
        ax0, ay0, ax1, ay1 = a.footprint()
        for b in items[i + 1:]:
            bx0, by0, bx1, by1 = b.footprint()
            if ax0 - pad < bx1 and bx0 - pad < ax1 and ay0 - pad < by1 and by0 - pad < ay1:
                problems.append(f"{a.key} overlaps {b.key}")
    if problems:
        raise SystemExit("Layout collision:\n  " + "\n  ".join(problems))


# --- svg primitives ---------------------------------------------------------
def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def draw_node(n):
    c = PALETTE[n.kind]
    out = [
        f'<rect x="{n.x}" y="{n.y}" width="{n.w}" height="{BOX_H}" rx="9" '
        f'fill="{c}" fill-opacity="0.16" stroke="{c}" stroke-width="1.7"/>',
        f'<rect x="{n.x}" y="{n.y}" width="4.5" height="{BOX_H}" rx="2.2" fill="{c}"/>',
    ]
    # Titles may be multi-line; centre the block vertically inside the box.
    tl = n.title_lines()
    ty = n.y + BOX_H / 2 + 4.5 - (len(tl) - 1) * 7.5
    for line in tl:
        out.append(
            f'<text x="{n.x + n.w / 2}" y="{ty}" text-anchor="middle" '
            f'font-family="Helvetica,Arial,sans-serif" font-size="{TITLE_SIZE}" '
            f'font-weight="600" fill="{FG}">{esc(line)}</text>'
        )
        ty += 15
    ty = n.y + BOX_H + 14
    for line in n.sub_lines():
        out.append(
            f'<text x="{n.x + n.w / 2}" y="{ty}" text-anchor="middle" '
            f'font-family="Helvetica,Arial,sans-serif" font-size="{SUB_SIZE}" '
            f'fill="{MUTED}">{esc(line)}</text>'
        )
        ty += LINE_H
    return out


def caption_half(n):
    """Half-width of the widest caption line hanging under a box."""
    if not n.sub_lines():
        return 0.0
    return max(len(ln) * SUB_SIZE * CHAR_W for ln in n.sub_lines()) / 2


def arrow(nodes, a, sa, b, sb, label="", dashed=False, via=None, off=0.0):
    # A vertical run between two boxes passes through the caption of whichever
    # box it leaves from below. Rather than eyeball the clearance, assert it:
    # the offset must sit outside the caption and still inside the box.
    if {sa, sb} <= {"t", "b"}:
        for key in (a, b):
            need = caption_half(nodes[key]) + 6
            limit = nodes[key].w / 2 - 5
            if abs(off) < need:
                raise SystemExit(
                    f"arrow {a}->{b}: offset {off} clips {key}'s caption "
                    f"(needs >{need:.0f}); shorten the caption or raise the offset"
                )
            if abs(off) > limit:
                raise SystemExit(
                    f"arrow {a}->{b}: offset {off} falls outside {key}'s box "
                    f"(max {limit:.0f}); shorten {key}'s caption"
                )

    x0, y0 = nodes[a].port(sa, off)
    x1, y1 = nodes[b].port(sb, off)
    if via == "h":       # horizontal, then vertical
        pts = f"M {x0} {y0} L {x1} {y0} L {x1} {y1}"
    elif via == "v":     # vertical, then horizontal
        pts = f"M {x0} {y0} L {x0} {y1} L {x1} {y1}"
    else:
        pts = f"M {x0} {y0} L {x1} {y1}"
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    out = [
        f'<path d="{pts}" fill="none" stroke="{EDGE}" stroke-width="1.6"'
        f'{dash} marker-end="url(#a)"/>'
    ]
    if label:
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        if via == "h":
            mx, my = (x0 + x1) / 2, y0
        elif via == "v":
            mx, my = x0, (y0 + y1) / 2
        w = len(label) * 9 * CHAR_W + 10
        out.append(
            f'<rect x="{mx - w / 2}" y="{my - 15}" width="{w}" height="14" rx="3" '
            f'fill="{BG}" fill-opacity="0.92"/>'
        )
        out.append(
            f'<text x="{mx}" y="{my - 4.5}" text-anchor="middle" '
            f'font-family="Helvetica,Arial,sans-serif" font-size="9" '
            f'fill="{MUTED}">{esc(label)}</text>'
        )
    return out


def band(x, y, w, h, label, colour):
    return [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{colour}" '
        f'fill-opacity="0.05" stroke="{colour}" stroke-opacity="0.32" '
        f'stroke-width="1" stroke-dasharray="5 4"/>',
        f'<text x="{x + 12}" y="{y + 17}" font-family="Helvetica,Arial,sans-serif" '
        f'font-size="10" font-weight="700" letter-spacing="1.1" '
        f'fill="{colour}">{esc(label)}</text>',
    ]


def document(width, height, title, subtitle, body):
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="{esc(title)}">',
        f'<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{EDGE}"/></marker></defs>',
        f'<rect width="{width}" height="{height}" fill="{BG}"/>',
        f'<text x="{MARGIN_X}" y="46" font-family="Helvetica,Arial,sans-serif" '
        f'font-size="21" font-weight="700" fill="{FG}">{esc(title)}</text>',
        f'<text x="{MARGIN_X}" y="68" font-family="Helvetica,Arial,sans-serif" '
        f'font-size="11.5" fill="{MUTED}">{esc(subtitle)}</text>',
        *body,
        "</svg>",
    ])


# --- diagram 1: system context ---------------------------------------------
def overview():
    N = {n.key: n for n in [
        Node("close",    0, 0, "Close CRM",      "webhook · invitee lifecycle", "external"),
        Node("cal",      0, 1, "Calendly",       "webhook · invitee.created", "external"),
        Node("spendsrc", 0, 2, "dea-data-bucket", "public S3 · daily spend files", "external"),
        Node("wistia",   0, 3, "Wistia Stats API", "by_date + events", "external"),

        Node("apigw",    1, 0, "API Gateway",    "POST /crm  ·  /calendly\nHMAC signature verified", "integrate"),
        Node("sched",    1, 2, "EventBridge",    "06:30 / 07:00 / 07:30 EST", "integrate"),

        Node("ingest",   2, 0, "crm-ingest\ncalendly-ingest", "natural-key object names", "compute"),
        Node("pull",     2, 2, "spend-ingest\nwistia-ingest", "manifest diff · self-healing", "compute"),

        Node("bronze",   3, 1, "insightflow-bronze", "immutable raw NDJSON\nthe replayable system of record", "storage"),

        Node("build",    4, 0, "warehouse-build", "orchestrates CTAS", "compute"),
        Node("athena",   4, 1, "Athena",         "CTAS full rebuild\nbuild-and-swap views", "analytics"),
        Node("glue",     4, 2, "Glue Catalog",   "3 databases", "analytics"),

        Node("silver",   5, 0, "insightflow-silver", "10 conformed tables", "storage"),
        Node("gold",     5, 1, "insightflow-gold", "8 metric marts", "storage"),
        Node("dash",     5, 2, "Streamlit",      "queries Gold", "serve"),
    ]}
    assert_no_collisions(N)

    # Bands are sized from the real content extent, never hardcoded — the old
    # diagram's boxes escaped their band because the height was a guess.
    top, bottom = 88, content_bounds(N)[3] + 18
    band_h = bottom - top

    body = []
    body += band(20, top, 3 * BOX_W + 2 * COL_GAP + 36, band_h,
                 "SOURCES  ·  INGEST", PALETTE["integrate"])
    body += band(MARGIN_X + 3 * (BOX_W + COL_GAP) - 26, top, BOX_W + 52, band_h,
                 "BRONZE", PALETTE["storage"])
    body += band(MARGIN_X + 4 * (BOX_W + COL_GAP) - 26, top,
                 2 * BOX_W + COL_GAP + 52, band_h,
                 "WAREHOUSE  ·  SERVE", PALETTE["analytics"])

    for n in N.values():
        body += draw_node(n)

    body += arrow(N, "close", "r", "apigw", "l")
    body += arrow(N, "cal", "r", "apigw", "l", via="v")
    body += arrow(N, "spendsrc", "r", "sched", "l")
    body += arrow(N, "wistia", "r", "sched", "l", via="v")
    body += arrow(N, "apigw", "r", "ingest", "l")
    body += arrow(N, "sched", "r", "pull", "l")
    body += arrow(N, "ingest", "r", "bronze", "l", via="h")
    body += arrow(N, "pull", "r", "bronze", "l", via="h")
    body += arrow(N, "bronze", "r", "athena", "l", "reads")
    # No label — "orchestrates CTAS" already sits under warehouse-build, and a
    # second caption on the same vertical run collides with it.
    body += arrow(N, "build", "b", "athena", "t", off=76)
    body += arrow(N, "athena", "b", "glue", "t", dashed=True, off=76)
    body += arrow(N, "athena", "r", "silver", "l", "CTAS", via="h")
    body += arrow(N, "athena", "r", "gold", "l", "CTAS")
    body += arrow(N, "gold", "b", "dash", "t", off=76)

    # The real-time branch lives on its own canvas; point at it rather than
    # cramming it in here, which is what made the old single diagram unreadable.
    callout_y = bottom + 22
    assert_below(N, callout_y, "overview callout")
    body.append(
        f'<rect x="{MARGIN_X}" y="{callout_y}" width="800" height="48" rx="8" '
        f'fill="{PALETTE["integrate"]}" fill-opacity="0.10" '
        f'stroke="{PALETTE["integrate"]}" stroke-opacity="0.5" stroke-width="1.4"/>'
    )
    body.append(
        f'<text x="{MARGIN_X + 16}" y="{callout_y + 21}" '
        f'font-family="Helvetica,Arial,sans-serif" '
        f'font-size="11.5" font-weight="600" fill="{FG}">'
        f'The CRM real-time alerting branch forks off Bronze →</text>'
    )
    body.append(
        f'<text x="{MARGIN_X + 16}" y="{callout_y + 38}" '
        f'font-family="Helvetica,Arial,sans-serif" '
        f'font-size="10.5" fill="{MUTED}">'
        f'10-min SQS delay, idempotency ledger, Slack alert, owner sweep. '
        f'See architecture-crm-realtime.svg</text>'
    )

    width = MARGIN_X + 6 * (BOX_W + COL_GAP) + 40
    return document(width, callout_y + 78, "InsightFlow — system context",
                    "AWS us-east-1 · account 995679261492 · Bronze/Silver/Gold medallion, "
                    "Athena CTAS full rebuild", body)


# --- diagram 2: CRM real-time path ------------------------------------------
def crm_realtime():
    # Grid chosen so every edge is a straight run: state stores sit on row 0,
    # the happy path on row 1, recovery on row 2. Nothing has to route around
    # a node, which is what turned the previous single canvas into spaghetti.
    N = {n.key: n for n in [
        Node("dlq",      4, 0, "DLQ",              "maxReceiveCount 5\ncap, never loop forever", "integrate"),
        Node("ledger",   5, 0, "event ledger",     "conditional PutItem\natomic claim, no TOCTOU", "database"),
        Node("cache",    6, 0, "lead-owner cache", "read-through\nnever negative-cached", "database"),

        Node("close",    0, 1, "Close CRM",        "webhook POST", "external"),
        Node("apigw",    1, 1, "API Gateway",      "HMAC-SHA256 verified\nhex key · ts + body", "integrate"),
        Node("ingest",   2, 1, "crm-ingest λ",     "lands raw, does nothing else", "compute"),
        Node("bronze",   3, 1, "insightflow-bronze", "crm_event_{event_id}.json\nkeyed on event_id", "storage"),
        Node("sqs",      4, 1, "SQS delay queue",  "10 min < 15 min cap\ncarries the pointer only", "integrate"),
        Node("enrich",   5, 1, "crm-enrich λ",     "claim → post → mark SENT", "compute"),
        Node("slack",    6, 1, "Slack",            "New Lead Alert", "external"),

        Node("cron",     2, 2, "EventBridge",      "hourly schedule", "integrate"),
        Node("sweep",    3, 2, "owner-sweep λ",    "drains the worklist", "compute"),
        Node("ownerbkt", 4, 2, "dea-lead-owner",   "public bucket\n403 = not yet assigned", "external"),
        Node("awaiting", 5, 2, "awaiting-owner",   "durable worklist\nretry_count → EXHAUSTED", "database"),
        Node("slackesc", 6, 2, "Slack",            "exhausted escalation", "external"),
    ]}
    assert_no_collisions(N)

    body = []
    for n in N.values():
        body += draw_node(n)

    body += arrow(N, "close", "r", "apigw", "l")
    body += arrow(N, "apigw", "r", "ingest", "l")
    body += arrow(N, "ingest", "r", "bronze", "l")
    body += arrow(N, "bronze", "r", "sqs", "l", "S3 event")
    body += arrow(N, "sqs", "r", "enrich", "l")
    body += arrow(N, "enrich", "r", "slack", "l", "alert")
    # Upward runs into row 0. Offset off-centre so they clear the captions
    # hanging beneath those boxes (see Node.port).
    body += arrow(N, "sqs", "t", "dlq", "b", off=82)
    body += arrow(N, "enrich", "t", "ledger", "b", off=82)
    body += arrow(N, "ledger", "r", "cache", "l", "owner?")
    # Recovery loop along row 2.
    body += arrow(N, "enrich", "b", "awaiting", "t", off=82)
    body += arrow(N, "cron", "r", "sweep", "l")
    body += arrow(N, "sweep", "r", "ownerbkt", "l", "re-read")
    body += arrow(N, "ownerbkt", "r", "awaiting", "l", "promote")
    body += arrow(N, "awaiting", "r", "slackesc", "l", "EXHAUSTED")

    notes = [
        "Send-then-mark-SENT: a duplicate page beats a missed lead, so the alert goes out before the ledger says it did.",
        "The PENDING lease is sized to worst-case invocation time (~120s), not to the 10-minute business delay — that is already spent in the queue.",
        "A missing owner is never negative-cached. It goes on the durable worklist, because “the next update will fix it” depends on an event that may never arrive.",
        "Both Slack destinations read one SSM SecureString; unset means the alert is logged, never silently dropped.",
    ]
    y = content_bounds(N)[3] + 42
    assert_below(N, y, "design notes")
    body.append(
        f'<text x="{MARGIN_X}" y="{y}" font-family="Helvetica,Arial,sans-serif" '
        f'font-size="10.5" font-weight="700" letter-spacing="1.1" '
        f'fill="{PALETTE["integrate"]}">DESIGN NOTES</text>'
    )
    y += 20
    for note in notes:
        body.append(
            f'<text x="{MARGIN_X}" y="{y}" font-family="Helvetica,Arial,sans-serif" '
            f'font-size="10.5" fill="{MUTED}">• {esc(note)}</text>'
        )
        y += 17

    width = MARGIN_X + 7 * (BOX_W + COL_GAP) + 40
    return document(width, y + 16, "InsightFlow — CRM real-time alerting path",
                    "Webhook to Slack in ~10 minutes, exactly-once-ish by deliberate choice", body)


def main():
    os.makedirs(ASSETS, exist_ok=True)
    for name, svg in (
        ("architecture-overview.svg", overview()),
        ("architecture-crm-realtime.svg", crm_realtime()),
    ):
        path = os.path.join(ASSETS, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg)
        print(f"wrote {os.path.relpath(path, REPO_ROOT)}")
    print("layout validated: no overlapping footprints")


if __name__ == "__main__":
    main()
