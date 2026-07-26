"""
Static checks on the dashboard.

    python3 tests/test_dashboard.py

Streamlit is not a dependency of the test suite — CI installs only boto3 — so
these are structural checks on the source rather than a rendering test. They
cover the two things most likely to break silently: the chart palette drifting
away from the validated values, and a display rule being softened into
something that reads as a number when it should read as "unknown".
"""

import ast
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO_ROOT, "streamlit")

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    line = f"  [{mark}] {name}"
    if detail and not condition:
        line += f"\n         {detail}"
    print(line)


def source(filename):
    with open(os.path.join(APP_DIR, filename)) as fh:
        return fh.read()


def test_sources_parse():
    print("\nDashboard - sources")
    for filename in ("app.py", "athena.py", "theme.py"):
        try:
            ast.parse(source(filename))
            ok = True
        except SyntaxError as exc:
            ok, detail = False, str(exc)
        check(f"{filename} parses", ok, detail if not ok else "")


def test_palette_is_the_validated_one():
    print("\nDashboard - palette")
    theme = source("theme.py")

    # These exact hexes were run through the colour validator for both modes.
    # Changing one without re-validating is how a palette quietly stops being
    # colourblind-safe, so the values are pinned here.
    light = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
    dark = ["#3987e5", "#d95926", "#199e70", "#c98500"]

    check("light series hexes are the validated set",
          all(h in theme for h in light),
          "re-run scripts/validate_palette.js before changing these")
    check("dark series hexes are the validated set",
          all(h in theme for h in dark),
          "dark is a separate validated stepping, not an automatic flip")

    check("channel order is fixed, not derived from the data",
          "CHANNEL_ORDER = [" in theme,
          "colour must follow the entity, so filtering must not repaint series")
    check("scale binds colours to a fixed domain",
          "alt.Scale(domain=CHANNEL_ORDER" in theme)

    check("sequential ramp is a single hue, light to dark",
          '"sequential"' in theme and "#cde2fb" in theme,
          "magnitude takes one hue; a rainbow implies categories")

    check("chrome uses ink tokens, not series colours",
          "text_primary" in theme and "muted" in theme)


def test_display_rules():
    print("\nDashboard - display rules that stop it lying")
    app = source("app.py")

    check("CPB rows with no value are dropped, not plotted as zero",
          "cpb[cpb[measure].notna()]" in app,
          "undefined is not free; rendering a divide-by-zero as $0 misleads")
    check("missing spend days are surfaced as an error, not silently totalled",
          "is_spend_data_missing" in app and "st.error" in app)
    check("funnel distinguishes 'not measurable' from 'measured zero'",
          "Not measurable" in app and "not** evidence" in app,
          "a 0% touch rate on a zero identification rate is unknowable, not zero")
    check("touch rate is presented as a lower bound",
          "lower bound" in app)
    check("meeting load states that per-host totals will not reconcile",
          "will not sum to total meetings" in app)
    check("proxied tenure is disclosed in the UI, not just the data",
          "tenure_is_proxied" in app and "proxied" in app)
    check("customer-preference view discloses its coverage",
          "perspective_coverage_rate" in app)

    check("blended CPB divides summed components",
          "total_spend / total_bookings" in app,
          "averaging channel CPBs weights a small channel like a large one")
    check("blended play rate divides summed components",
          'totals["play_count"].sum() / totals["load_count"].sum()' in app)


def test_accessibility():
    print("\nDashboard - accessibility")
    app = source("app.py")

    # Two light-mode hues sit below 3:1 against the surface, so the relief rule
    # applies: the numbers must be reachable without reading the chart.
    check("a table view accompanies the charts",
          "def table_view" in app and app.count("table_view(") >= 6,
          "relief rule for the sub-3:1 light-mode hues")
    check("legends are present for multi-series charts",
          app.count("alt.Legend(title=") >= 5,
          "identity must never be carried by colour alone")
    check("every chart declares tooltips",
          app.count("tooltip=[") >= 6)
    check("dark mode is a selected palette, not an inversion",
          "palette(dark_mode)" in app)


def test_no_metric_logic_leaks_into_the_view():
    print("\nDashboard - separation of concerns")
    app = source("app.py")

    # The dashboard may re-aggregate for a headline tile, but it must not
    # reimplement a metric — two consumers of one mart would then disagree.
    banned = [
        (r"INTERVAL\s+'\d+'\s+DAY", "attribution window belongs in the funnel CTAS"),
        (r"\bFULL OUTER JOIN\b", "the spend join belongs in the CPB mart"),
        (r"received_at.*<.*created_at", "the temporal guard belongs in SQL"),
    ]
    leaks = [why for pattern, why in banned if re.search(pattern, app, re.IGNORECASE)]
    check("no metric logic reimplemented in the view", not leaks, f"{leaks}")

    athena_src = source("athena.py")
    check("the query helper stays thin",
          "def run_query" in athena_src and "GROUP BY" not in athena_src.upper())


def test_degrades_without_aws():
    print("\nDashboard - behaviour before deployment")
    app = source("app.py")
    check("query failures render an explanation, not a stack trace",
          "def unavailable" in app and "except athena.QueryError" in app,
          "the app must be runnable before the warehouse has ever built")
    check("empty results are handled separately from errors",
          ".empty" in app and "st.warning" in app)


if __name__ == "__main__":
    test_sources_parse()
    test_palette_is_the_validated_one()
    test_display_rules()
    test_accessibility()
    test_no_metric_logic_leaks_into_the_view()
    test_degrades_without_aws()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
