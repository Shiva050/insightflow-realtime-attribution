"""
Chart palette and shared Altair helpers.

The categorical slots and chrome values come from a validated palette. Both
modes were run through the colour validator for the four channel series
actually used here:

    light  worst adjacent CVD ΔE 9.1, normal-vision 22.9   PASS
    dark   worst adjacent CVD ΔE 8.4, normal-vision 19.8   PASS

Light mode raises a contrast warning on two hues against the light surface, so
the relief rule applies: every chart ships a legend AND a table view, and no
chart carries meaning by colour alone.
"""

import altair as alt

# Fixed slot order. Channels are assigned by identity, never by rank — a filter
# that drops a channel must not repaint the survivors.
CHANNEL_ORDER = ["facebook_paid_ads", "youtube_paid_ads", "tiktok_paid_ads", "other"]

CHANNEL_LABELS = {
    "facebook_paid_ads": "Facebook",
    "youtube_paid_ads": "YouTube",
    "tiktok_paid_ads": "TikTok",
    "other": "Other / organic",
}

LIGHT = {
    "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"],
    "surface": "#fcfcfb",
    "text_primary": "#0b0b0b",
    "text_secondary": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "sequential": ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#104281"],
    "good": "#0ca30c",
    "warning": "#fab219",
    "critical": "#d03b3b",
}

DARK = {
    "series": ["#3987e5", "#d95926", "#199e70", "#c98500"],
    "surface": "#1a1a19",
    "text_primary": "#ffffff",
    "text_secondary": "#c3c2b7",
    "muted": "#898781",
    "grid": "#2c2c2a",
    "axis": "#383835",
    "sequential": ["#104281", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4"],
    "good": "#0ca30c",
    "warning": "#fab219",
    "critical": "#d03b3b",
}


def palette(dark_mode):
    return DARK if dark_mode else LIGHT


def channel_scale(p):
    """Colour follows the entity. Same channel, same hue, on every chart."""
    return alt.Scale(domain=CHANNEL_ORDER, range=p["series"])


def base_config(chart, p):
    """Recessive chrome: hairline grid, muted axes, text in ink not series colour."""
    return (
        chart.configure_view(strokeWidth=0, fill=p["surface"])
        .configure_axis(
            grid=True,
            gridColor=p["grid"],
            gridWidth=1,
            domainColor=p["axis"],
            tickColor=p["axis"],
            labelColor=p["muted"],
            titleColor=p["text_secondary"],
            labelFontSize=11,
            titleFontSize=12,
            titleFontWeight="normal",
        )
        .configure_legend(
            labelColor=p["text_secondary"],
            titleColor=p["text_secondary"],
            labelFontSize=11,
            titleFontSize=11,
            symbolType="square",
            symbolSize=90,
        )
        .configure_title(color=p["text_primary"], fontSize=13, fontWeight=600, anchor="start")
    )


def pretty_channels(df, column="channel"):
    if column in df.columns:
        df = df.copy()
        df[column + "_label"] = df[column].map(CHANNEL_LABELS).fillna(df[column])
    return df
