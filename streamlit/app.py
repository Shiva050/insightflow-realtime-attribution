"""
InsightFlow dashboard.

Reads Gold marts and displays them. There is deliberately no metric logic here:
every rate was divided once in a CTAS, so two consumers of the same mart cannot
disagree, and a number on screen can always be traced to a table.

The dashboard's second job is to make coverage visible. A figure built on a
partial join is not wrong so much as incomplete, and an incomplete figure
presented bare is how a confident wrong number reaches a stakeholder. So every
cross-source number here carries its coverage, and metrics that cannot be
computed say so instead of rendering a zero.
"""

import altair as alt
import pandas as pd
import streamlit as st

import athena
from theme import CHANNEL_LABELS, base_config, channel_scale, palette, pretty_channels

st.set_page_config(page_title="InsightFlow", page_icon="📊", layout="wide")

# --- chrome ----------------------------------------------------------------
with st.sidebar:
    st.markdown("### InsightFlow")
    dark_mode = st.toggle("Dark charts", value=False)
    st.divider()
    st.caption(
        "Marts rebuild daily. Historical figures can improve retroactively as "
        "late enrichment lands — that is correct behaviour, which is why every "
        "row carries a build date."
    )
    if st.button("Clear cache and refetch"):
        st.cache_data.clear()
        st.rerun()

P = palette(dark_mode)


def load(fn, *args, **kwargs):
    """Query, or explain what is missing instead of throwing a stack trace."""
    try:
        return fn(*args, **kwargs), None
    except athena.QueryError as exc:
        return pd.DataFrame(), str(exc)
    except Exception as exc:  # credentials, region, network
        return pd.DataFrame(), str(exc)


def unavailable(name, error):
    """
    Explain what is actually wrong.

    The raw boto3 error for absent credentials is a MissingDependency notice
    about botocore[crt], which sends people to install a package they do not
    need. Classify first, show the raw text second.
    """
    lowered = str(error).lower()

    if "credential" in lowered or "token" in lowered or "missing dependency" in lowered:
        cause = (
            "**AWS credentials are not configured for this session.** "
            "Run `aws sso login` (or set the usual environment variables) and "
            "reload."
        )
    elif "does not exist" in lowered or "not found" in lowered or "table" in lowered:
        cause = (
            "**The warehouse has not been built yet.** Run the "
            "`insightflow-warehouse-build` Lambda, then reload."
        )
    elif "access denied" in lowered or "authoriz" in lowered:
        cause = (
            "**Permission denied.** This role needs Athena execute, Glue read, "
            "and S3 read on the Silver and Gold buckets."
        )
    else:
        cause = "The query did not complete."

    st.info(f"**{name}** is not available yet.\n\n{cause}")
    with st.expander("Error detail"):
        st.code(str(error), language="text")


def table_view(df, key):
    """
    The relief rule: two series hues sit below 3:1 on the light surface, so
    every chart is accompanied by the underlying numbers. Also the accessible
    path for anyone who cannot use the chart at all.
    """
    with st.expander("Show the numbers", expanded=False):
        st.dataframe(df, use_container_width=True, hide_index=True, key=key)


# ---------------------------------------------------------------------------
tab_overview, tab_bookings, tab_cost, tab_timing, tab_team, tab_video, tab_quality = st.tabs(
    ["Overview", "Bookings", "Cost", "Timing", "Team", "Video & funnel", "Data quality"]
)


# ===========================================================================
# Overview
# ===========================================================================
with tab_overview:
    st.subheader("Overview")

    attribution, err = load(athena.gold, "channel_attribution", order_by="rank_by_volume")

    if err:
        unavailable("Channel attribution", err)
    elif attribution.empty:
        st.warning("No bookings have been ingested yet.")
    else:
        total_bookings = int(attribution["total_bookings"].sum())
        total_spend = float(attribution["total_spend"].fillna(0).sum())
        # Divided once, from summed components — never an average of per-channel
        # CPB, which would weight a 1-booking channel like a 40-booking one.
        blended_cpb = total_spend / total_bookings if total_bookings else None

        # Coverage over channels where spend is EXPECTED. Organic bookings land
        # under 'other', which has no spend by definition — including it would
        # drag the headline toward zero and report a data-quality failure that
        # has not happened. Weighted by days observed so a channel with one day
        # of history does not swing the figure.
        paid = attribution[attribution["total_spend"].notna()]
        if not paid.empty and paid["days_observed"].sum():
            covered_days = (paid["days_observed"] - paid["days_spend_missing"]).sum()
            coverage = covered_days / paid["days_observed"].sum()
        else:
            coverage = float("nan")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Booked calls", f"{total_bookings:,}")
        c2.metric("Ad spend", f"${total_spend:,.0f}")
        c3.metric("Blended CPB", f"${blended_cpb:,.2f}" if blended_cpb else "—",
                  help="Total spend ÷ total bookings, computed from summed "
                       "components rather than by averaging channel CPBs.")
        c4.metric("Spend coverage", f"{coverage:.0%}" if pd.notna(coverage) else "—",
                  help="Share of days where a spend file was actually landed. "
                       "Days with missing spend are excluded from totals rather "
                       "than counted as zero.")

        if pd.notna(coverage) and coverage < 1:
            st.warning(
                f"Spend data is missing for some days (coverage {coverage:.0%}). "
                "Those days are excluded from spend totals — they are **not** "
                "treated as zero-spend, because a failed ingestion would "
                "otherwise make a channel look free."
            )

        df = pretty_channels(attribution)
        chart = (
            alt.Chart(df, height=280, title="Bookings by channel")
            .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=38)
            .encode(
                x=alt.X("channel_label:N", title=None, sort="-y",
                        axis=alt.Axis(labelAngle=0)),
                y=alt.Y("total_bookings:Q", title="Booked calls"),
                color=alt.Color("channel:N", scale=channel_scale(P),
                                legend=alt.Legend(title="Channel",
                                                  labelExpr="datum.value")),
                tooltip=[
                    alt.Tooltip("channel_label:N", title="Channel"),
                    alt.Tooltip("total_bookings:Q", title="Bookings", format=","),
                    alt.Tooltip("total_spend:Q", title="Spend", format="$,.2f"),
                    alt.Tooltip("cpb:Q", title="CPB", format="$,.2f"),
                    alt.Tooltip("spend_coverage_rate:Q", title="Spend coverage",
                                format=".0%"),
                ],
            )
        )
        st.altair_chart(base_config(chart, P), use_container_width=True)
        table_view(attribution, "overview_tbl")


# ===========================================================================
# Bookings
# ===========================================================================
with tab_bookings:
    st.subheader("Daily calls booked by source")

    trend, err = load(athena.gold, "bookings_trend", order_by="booking_date")

    if err:
        unavailable("Bookings trend", err)
    elif trend.empty:
        st.warning("No bookings yet.")
    else:
        trend = pretty_channels(trend)
        smooth = st.checkbox("7-day rolling average", value=False,
                             help="Daily booking counts are noisy at low volume.")
        measure = "bookings_7d_rolling" if smooth else "bookings"

        line = (
            alt.Chart(trend, height=320,
                      title="Bookings per day" + (" (7-day rolling)" if smooth else ""))
            .mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=28, filled=True))
            .encode(
                x=alt.X("booking_date:T", title=None),
                y=alt.Y(f"{measure}:Q", title="Booked calls"),
                color=alt.Color("channel:N", scale=channel_scale(P),
                                legend=alt.Legend(title="Channel")),
                tooltip=[
                    alt.Tooltip("booking_date:T", title="Date"),
                    alt.Tooltip("channel_label:N", title="Channel"),
                    alt.Tooltip("bookings:Q", title="Bookings"),
                    alt.Tooltip("bookings_excl_canceled:Q", title="Excl. cancelled"),
                ],
            )
        )
        st.altair_chart(base_config(line, P), use_container_width=True)
        st.caption(
            "Days with no bookings are plotted as zero rather than skipped — a "
            "line drawn across a gap reads as 'steady' when nothing happened."
        )
        table_view(trend, "trend_tbl")

        st.divider()
        st.subheader("Cumulative volume")
        area = (
            alt.Chart(trend, height=260)
            .mark_area(opacity=0.85, line={"strokeWidth": 2})
            .encode(
                x=alt.X("booking_date:T", title=None),
                y=alt.Y("bookings_cumulative:Q", title="Cumulative bookings",
                        stack=None),
                color=alt.Color("channel:N", scale=channel_scale(P),
                                legend=alt.Legend(title="Channel")),
                tooltip=[alt.Tooltip("booking_date:T", title="Date"),
                         alt.Tooltip("channel_label:N", title="Channel"),
                         alt.Tooltip("bookings_cumulative:Q", title="Cumulative")],
            )
        )
        st.altair_chart(base_config(area, P), use_container_width=True)


# ===========================================================================
# Cost
# ===========================================================================
with tab_cost:
    st.subheader("Cost per booking")

    cpb, err = load(athena.gold, "cpb_by_channel", order_by="metric_date")

    if err:
        unavailable("CPB", err)
    elif cpb.empty:
        st.warning("No spend or booking data yet.")
    else:
        cpb = pretty_channels(cpb)

        missing_days = int(cpb["is_spend_data_missing"].fillna(False).sum())
        if missing_days:
            st.error(
                f"**{missing_days} channel-day(s) have no spend data landed.** "
                "CPB is suppressed for those rows rather than computed. A missing "
                "spend row is only 'organic' if the ingestion manifest confirms "
                "the file was landed — otherwise reporting it would make a "
                "channel look infinitely efficient *because* the pipeline broke."
            )

        rolling = st.checkbox(
            "7-day rolling CPB", value=True,
            help="Daily CPB assumes same-day conversion. Real ad response lags, "
                 "so Monday's spend can drive a Wednesday booking — daily CPB is "
                 "noisy by construction.")
        measure = "cpb_7d_rolling" if rolling else "cpb"

        plot = cpb[cpb[measure].notna()]
        line = (
            alt.Chart(plot, height=320,
                      title="CPB over time" + (" (7-day rolling)" if rolling else ""))
            .mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=28, filled=True))
            .encode(
                x=alt.X("metric_date:T", title=None),
                y=alt.Y(f"{measure}:Q", title="Cost per booking (USD)",
                        axis=alt.Axis(format="$,.0f")),
                color=alt.Color("channel:N", scale=channel_scale(P),
                                legend=alt.Legend(title="Channel")),
                tooltip=[
                    alt.Tooltip("metric_date:T", title="Date"),
                    alt.Tooltip("channel_label:N", title="Channel"),
                    alt.Tooltip("spend:Q", title="Spend", format="$,.2f"),
                    alt.Tooltip("bookings:Q", title="Bookings"),
                    alt.Tooltip("cpb:Q", title="CPB", format="$,.2f"),
                ],
            )
        )
        st.altair_chart(base_config(line, P), use_container_width=True)
        st.caption(
            "Days with zero bookings are omitted, not plotted at zero: CPB is "
            "**undefined** when nothing was booked, and rendering that as $0 "
            "would read as 'free'."
        )
        table_view(cpb, "cpb_tbl")

        st.divider()
        st.subheader("Channel leaderboard")
        board, berr = load(athena.gold, "channel_attribution",
                           order_by="rank_by_efficiency")
        if berr:
            unavailable("Leaderboard", berr)
        elif not board.empty:
            display = pretty_channels(board)[[
                "channel_label", "total_bookings", "total_spend", "cpb",
                "cpb_excl_canceled", "spend_coverage_rate", "days_spend_missing",
            ]].rename(columns={
                "channel_label": "Channel",
                "total_bookings": "Bookings",
                "total_spend": "Spend",
                "cpb": "CPB",
                "cpb_excl_canceled": "CPB excl. cancelled",
                "spend_coverage_rate": "Spend coverage",
                "days_spend_missing": "Days missing spend",
            })
            st.dataframe(
                display, use_container_width=True, hide_index=True,
                column_config={
                    "Spend": st.column_config.NumberColumn(format="$%.2f"),
                    "CPB": st.column_config.NumberColumn(format="$%.2f"),
                    "CPB excl. cancelled": st.column_config.NumberColumn(format="$%.2f"),
                    "Spend coverage": st.column_config.NumberColumn(format="%.0f%%"),
                },
            )
            st.caption(
                "Channel CPB is recomputed from summed spend and summed bookings, "
                "not averaged from daily CPB — averaging would weight a "
                "one-booking day the same as a forty-booking day."
            )


# ===========================================================================
# Timing
# ===========================================================================
with tab_timing:
    st.subheader("When calls are booked")
    st.caption(
        "This is two questions, not one. **Customer preference** uses the "
        "invitee's local hour — a noon PST call and a 4pm CST call are both "
        "'early afternoon' to the person booking. **Staffing demand** uses the "
        "business hour, because those same two calls hit the team at different "
        "moments. One timezone cannot answer both."
    )

    slots, err = load(athena.gold, "booking_time_slots")

    if err:
        unavailable("Time slots", err)
    elif slots.empty:
        st.warning("No bookings yet.")
    else:
        perspective = st.radio(
            "Perspective", ["customer_local", "business_est"], horizontal=True,
            format_func=lambda v: {"customer_local": "Customer preference (invitee local time)",
                                   "business_est": "Staffing demand (business EST)"}[v])

        view = slots[slots["perspective"] == perspective]

        if perspective == "customer_local" and not view.empty:
            cov = float(view["perspective_coverage_rate"].iloc[0])
            if cov < 1:
                st.warning(
                    f"Built on {cov:.0%} of bookings — the rest carry no invitee "
                    "timezone. They are excluded rather than defaulted to EST, "
                    "which would relabel someone else's morning as ours."
                )

        dow_names = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
        view = view.copy()
        view["dow_label"] = view["day_of_week"].map(dow_names).fillna(view["day_of_week"])

        heat = (
            alt.Chart(view, height=300, title="Bookings by hour and weekday")
            .mark_rect(cornerRadius=2)
            .encode(
                x=alt.X("hour_of_day:O", title="Hour of day"),
                y=alt.Y("dow_label:N", title=None,
                        sort=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]),
                # Sequential: one hue, light to dark. Magnitude, not identity.
                color=alt.Color("sum(bookings):Q", title="Bookings",
                                scale=alt.Scale(range=P["sequential"])),
                tooltip=[alt.Tooltip("dow_label:N", title="Day"),
                         alt.Tooltip("hour_of_day:O", title="Hour"),
                         alt.Tooltip("sum(bookings):Q", title="Bookings")],
            )
        )
        st.altair_chart(base_config(heat, P), use_container_width=True)
        table_view(view, "slots_tbl")


# ===========================================================================
# Team
# ===========================================================================
with tab_team:
    st.subheader("Meeting load per employee")

    load_df, err = load(athena.gold, "meeting_load_by_employee", order_by="rank_by_load")

    if err:
        unavailable("Meeting load", err)
    elif load_df.empty:
        st.warning("No host data yet.")
    else:
        st.info(
            "**Per-employee load will not sum to total meetings, and that is "
            "correct.** A co-hosted meeting is one meeting and one unit of load "
            "on *each* host."
        )
        if bool(load_df["tenure_is_proxied"].iloc[0]):
            st.warning(
                "Weeks are **proxied** as first-meeting to period-end. True "
                "tenure needs an HR roster with start and leave dates, which "
                "Calendly does not provide. The proxy misses pre-hire idle time "
                "and anyone who has left."
            )

        c1, c2, c3 = st.columns(3)
        c1.metric("Hosts", f"{len(load_df):,}")
        c2.metric("Busiest", f"{load_df['avg_meetings_per_week'].max():.1f}/wk")
        c3.metric("Quietest", f"{load_df['avg_meetings_per_week'].min():.1f}/wk")

        bar = (
            alt.Chart(load_df, height=max(240, 32 * len(load_df)),
                      title="Average meetings per week")
            .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, size=20,
                      color=P["series"][0])
            .encode(
                x=alt.X("avg_meetings_per_week:Q", title="Meetings per week"),
                y=alt.Y("host_name:N", title=None, sort="-x"),
                tooltip=[
                    alt.Tooltip("host_name:N", title="Host"),
                    alt.Tooltip("total_meetings:Q", title="Total meetings"),
                    alt.Tooltip("tenure_weeks:Q", title="Tenure weeks (proxy)"),
                    alt.Tooltip("avg_meetings_per_week:Q", title="Per week", format=".2f"),
                    alt.Tooltip("avg_per_week_naive_window:Q",
                                title="If divided by window", format=".2f"),
                ],
            )
        )
        st.altair_chart(base_config(bar, P), use_container_width=True)
        st.caption(
            "Idle weeks stay in the denominator. Counting only weeks with a "
            "meeting cannot reveal underload — an idle week would drop out, so "
            "an idle host and a slammed one could show the same average."
        )
        table_view(load_df, "load_tbl")


# ===========================================================================
# Video & funnel
# ===========================================================================
with tab_video:
    st.subheader("Video engagement")

    media, err = load(athena.gold, "media_engagement", order_by="stat_date")

    if err:
        unavailable("Media engagement", err)
    elif media.empty:
        st.warning("No Wistia data yet.")
    else:
        totals = media.groupby("media_name", as_index=False)[
            ["load_count", "play_count"]].sum()
        totals["play_rate"] = totals["play_count"] / totals["load_count"]

        cols = st.columns(len(totals) + 1)
        for i, row in totals.iterrows():
            cols[i].metric(str(row["media_name"])[:28],
                           f"{row['play_rate']:.2%}",
                           help=f"{int(row['play_count']):,} plays / "
                                f"{int(row['load_count']):,} loads")
        blended = totals["play_count"].sum() / totals["load_count"].sum()
        cols[-1].metric("Blended play rate", f"{blended:.2%}",
                        help="Summed plays ÷ summed loads. Averaging the "
                             "per-video rates would give a very different and "
                             "wrong answer, because one video has vastly more "
                             "loads than the other.")

        line = (
            alt.Chart(media, height=300, title="Play rate over time (7-day)")
            .mark_line(strokeWidth=2)
            .encode(
                x=alt.X("stat_date:T", title=None),
                y=alt.Y("play_rate_7d:Q", title="Play rate",
                        axis=alt.Axis(format=".1%")),
                color=alt.Color("media_name:N",
                                scale=alt.Scale(range=P["series"]),
                                legend=alt.Legend(title="Video")),
                tooltip=[alt.Tooltip("stat_date:T", title="Date"),
                         alt.Tooltip("media_name:N", title="Video"),
                         alt.Tooltip("load_count:Q", title="Loads"),
                         alt.Tooltip("play_count:Q", title="Plays"),
                         alt.Tooltip("play_rate_7d:Q", title="Play rate (7d)",
                                     format=".2%")],
            )
        )
        st.altair_chart(base_config(line, P), use_container_width=True)
        st.caption(
            "Rolling play rate divides summed plays by summed loads. It is never "
            "an average of daily rates, which would weight a 3-load day the same "
            "as a 3,000-load day."
        )
        table_view(media, "media_tbl")

    st.divider()
    st.subheader("Video → booking funnel")

    funnel, ferr = load(athena.gold, "video_booking_funnel", order_by="booking_date DESC")

    if ferr:
        unavailable("Funnel", ferr)
    elif funnel.empty:
        st.warning("No funnel data yet.")
    else:
        ident = funnel["video_identification_rate"].dropna()
        ident_rate = float(ident.iloc[0]) if not ident.empty else 0.0

        if ident_rate == 0:
            st.error(
                "**Not measurable.** No Wistia session carries an email address, "
                "so the video→lead bridge has nothing to join on.\n\n"
                "This is **not** evidence that video drove no bookings — it means "
                "we cannot tell either way. The two are different claims, and "
                "only this coverage figure distinguishes them."
            )
        else:
            st.info(
                f"Video identification rate is **{ident_rate:.1%}**. The touch "
                "rate below is a **lower bound**: anonymous watchers cannot be "
                "matched, so true video influence is at least this."
            )

        agg = funnel.groupby("channel", as_index=False)[
            ["bookings", "bookings_with_email", "bookings_with_prior_video"]].sum()
        agg = pretty_channels(agg)
        agg["email_coverage"] = agg["bookings_with_email"] / agg["bookings"]

        st.dataframe(
            agg[["channel_label", "bookings", "bookings_with_email",
                 "email_coverage", "bookings_with_prior_video"]].rename(columns={
                     "channel_label": "Channel",
                     "bookings": "Bookings",
                     "bookings_with_email": "With email",
                     "email_coverage": "Email coverage",
                     "bookings_with_prior_video": "With prior video (floor)",
                 }),
            use_container_width=True, hide_index=True,
            column_config={"Email coverage": st.column_config.NumberColumn(format="%.0f%%")},
        )
        st.caption(
            "The funnel is built backward from bookings, not forward from video. "
            "Every booking has a channel and a known denominator; the video top "
            "is mostly anonymous, so a forward conversion rate would have an "
            "unknowable denominator."
        )


# ===========================================================================
# Data quality
# ===========================================================================
with tab_quality:
    st.subheader("Coverage and enrichment")
    st.caption(
        "Coverage is a monitored metric, not a one-time check. A match rate "
        "sliding from 85% to 60% overnight means the owner sweep broke — and "
        "you want to learn that here, not from a stakeholder."
    )

    dq, err = load(athena.silver, "dq_lead_coverage", order_by="build_date")

    if err:
        unavailable("Lead coverage", err)
    elif dq.empty:
        st.warning("No coverage history yet — it accumulates one row per build.")
    else:
        latest = dq.iloc[-1]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Leads", f"{int(latest['total_leads']):,}")
        c2.metric("Email match rate",
                  f"{float(latest['email_match_rate']):.0%}"
                  if pd.notna(latest["email_match_rate"]) else "—")
        c3.metric("Awaiting owner", f"{int(latest['awaiting_owner_open']):,}")
        c4.metric("Exhausted", f"{int(latest['awaiting_owner_exhausted']):,}",
                  help="Leads whose owner never arrived after the full retry "
                       "budget. These need a human.")

        if int(latest["awaiting_owner_exhausted"]) > 0:
            st.error(
                f"{int(latest['awaiting_owner_exhausted'])} lead(s) exhausted "
                "their owner-lookup retries. The sweep has given up on them by "
                "design — it caps retries rather than polling forever — and they "
                "now need manual attention."
            )

        trend_chart = (
            alt.Chart(dq, height=260, title="Email match rate over time")
            .mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=32, filled=True),
                       color=P["series"][0])
            .encode(
                x=alt.X("build_date:T", title=None),
                y=alt.Y("email_match_rate:Q", title="Match rate",
                        axis=alt.Axis(format=".0%"),
                        scale=alt.Scale(domain=[0, 1])),
                tooltip=[alt.Tooltip("build_date:T", title="Build"),
                         alt.Tooltip("total_leads:Q", title="Leads"),
                         alt.Tooltip("email_match_rate:Q", title="Match rate",
                                     format=".1%"),
                         alt.Tooltip("awaiting_owner_open:Q", title="Awaiting")],
            )
        )
        st.altair_chart(base_config(trend_chart, P), use_container_width=True)
        st.caption(str(latest.get("coverage_caveat", "")))
        table_view(dq, "dq_tbl")
