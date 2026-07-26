-- =============================================================================
-- channel_attribution  ·  grain: channel  ·  the leaderboard
-- =============================================================================
-- Total bookings, total spend and CPB per channel, ranked.
--
-- RE-AGGREGATED FROM COMPONENTS, NOT FROM DAILY CPB.
-- The daily mart already computes a CPB per day. Averaging those to get a
-- channel figure would weight a 1-booking day the same as a 40-booking day. So
-- this sums spend and sums bookings across the window and divides once, which
-- is the same non-additive-ratio rule applied one grain up. Carrying the
-- components in the daily mart is precisely what makes that possible.
--
-- DAYS WITH BROKEN SPEND DATA ARE EXCLUDED FROM THE SPEND TOTAL, not treated as
-- zero. Including them would understate spend and flatter the channel.
--
-- The spec asks for attribution "using UTM parameters". Every UTM field was
-- null in the source, so channel comes from the event_type map instead. The
-- UTM columns are carried in Silver so that assumption stays checkable.
-- =============================================================================

CREATE TABLE insightflow_gold.channel_attribution__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{gold_bucket}}/channel_attribution/build_id={{build_id}}/'
) AS

WITH per_channel AS (
  SELECT
    channel,

    SUM(bookings)                                       AS total_bookings,
    SUM(bookings_excl_canceled)                         AS total_bookings_excl_canceled,

    -- Only days we can vouch for contribute to the spend total.
    SUM(CASE WHEN NOT is_spend_data_missing THEN spend END)  AS total_spend,

    COUNT(*)                                            AS days_observed,
    COUNT_IF(is_spend_data_missing)                     AS days_spend_missing,
    COUNT_IF(is_genuine_zero_spend)                     AS days_zero_spend,

    MIN(metric_date)                                    AS first_date,
    MAX(metric_date)                                    AS last_date
  FROM insightflow_gold.cpb_by_channel__{{build_id}}
  GROUP BY channel
)

SELECT
  channel,
  total_bookings,
  total_bookings_excl_canceled,
  total_spend,

  -- Divided once, from summed components.
  CASE WHEN total_bookings > 0 AND total_spend IS NOT NULL
       THEN ROUND(total_spend / CAST(total_bookings AS DOUBLE), 2)
  END                                                   AS cpb,

  CASE WHEN total_bookings_excl_canceled > 0 AND total_spend IS NOT NULL
       THEN ROUND(total_spend / CAST(total_bookings_excl_canceled AS DOUBLE), 2)
  END                                                   AS cpb_excl_canceled,

  -- Ranks are on the leaderboard, so the dashboard does not re-derive them and
  -- risk a different tie-break.
  RANK() OVER (ORDER BY total_bookings DESC)            AS rank_by_volume,
  RANK() OVER (
    ORDER BY CASE WHEN total_bookings > 0 AND total_spend IS NOT NULL
                  THEN total_spend / CAST(total_bookings AS DOUBLE) END
    ASC NULLS LAST
  )                                                     AS rank_by_efficiency,

  days_observed,
  days_spend_missing,
  days_zero_spend,
  -- Coverage rides along with the figure, so a CPB built on 3 of 30 days is
  -- visibly that rather than looking equally solid as one built on all 30.
  ROUND(CAST(days_observed - days_spend_missing AS DOUBLE)
        / NULLIF(days_observed, 0), 4)                  AS spend_coverage_rate,

  first_date,
  last_date,
  DATE '{{asof}}'                                       AS build_date

FROM per_channel;
