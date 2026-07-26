-- =============================================================================
-- cpb_by_channel  ·  grain: metric_date + channel
-- =============================================================================
-- Cost Per Booking. Three traps live in this one metric.
--
-- 1. THE FAN-OUT. fct_spend has ONE row per (date, channel); fct_booking has
--    one row per meeting. Joining them directly fans the spend value across
--    every booking row — 10 bookings turns $653.28 into SUM = $6,532.80 and
--    inflates spend 10x. Nothing errors; the dashboard just lies. So bookings
--    are collapsed to (date, channel) BEFORE the join, and the join is
--    one-to-one. Grains must match AT JOIN TIME, not merely at divide time.
--
-- 2. NON-ADDITIVE RATIO. CPB is carried as components — spend and bookings —
--    and divided exactly once, here, at the grain the dashboard displays. A
--    consumer summing CPB across channels would get nonsense, which is why the
--    components ship alongside it.
--
-- 3. ABSENCE IS NOT ZERO. "Bookings but no spend row" has two opposite causes.
--    If our Bronze covers that date, it is a genuine zero — organic bookings, a
--    real finding. If it does not, the pull broke and the metric must be
--    suppressed. Rendering a failed ingestion as "organic" makes a channel look
--    infinitely efficient BECAUSE the pipeline failed.
--
-- FULL OUTER JOIN so neither side's rows are dropped. Spend with zero bookings
-- is a valid market outcome; bookings with no spend is a real finding. An inner
-- join would silently hide exactly the days that need attention.
--
-- CPB is UNDEFINED when bookings are zero — never 0, never infinity. Rendering
-- a divide-by-zero as 0 reads as "free", which is a lie.
--
-- KNOWN LIMITATION: same-day attribution assumes same-day conversion. Real ad
-- response lags, so Monday's ad can drive a Wednesday booking. Daily CPB is
-- noisy by construction; cpb_7d_rolling is the figure to lean on.
-- =============================================================================

CREATE TABLE insightflow_gold.cpb_by_channel__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{gold_bucket}}/cpb_by_channel/build_id={{build_id}}/'
) AS

WITH bookings_agg AS (
  -- Collapsed to the spend grain BEFORE the join. This is the whole defence
  -- against trap 1.
  SELECT
    booking_date_est                          AS metric_date,
    channel,
    COUNT(*)                                  AS bookings,
    COUNT_IF(NOT is_canceled)                 AS bookings_excl_canceled
  FROM insightflow_silver.fct_booking
  GROUP BY booking_date_est, channel
),

spend_agg AS (
  SELECT
    spend_date                                AS metric_date,
    channel,
    SUM(spend)                                AS spend
  FROM insightflow_silver.fct_spend
  GROUP BY spend_date, channel
),

-- Bronze's own coverage plays the manifest role from S14: a date we landed a
-- file for is a date we can speak about. Every spend file carries a 30-day
-- window, so any date inside a landed window appears here.
spend_coverage AS (
  SELECT DISTINCT spend_date AS metric_date
  FROM insightflow_silver.fct_spend
),

joined AS (
  SELECT
    COALESCE(b.metric_date, s.metric_date)    AS metric_date,
    COALESCE(b.channel, s.channel)            AS channel,
    COALESCE(b.bookings, 0)                   AS bookings,
    COALESCE(b.bookings_excl_canceled, 0)     AS bookings_excl_canceled,
    s.spend
  FROM bookings_agg b
  FULL OUTER JOIN spend_agg s
    ON  s.metric_date = b.metric_date
    AND s.channel     = b.channel
)

SELECT
  j.metric_date,
  j.channel,

  -- Components travel with the ratio so it can be re-aggregated correctly at
  -- any other grain.
  j.spend,
  j.bookings,
  j.bookings_excl_canceled,

  -- Trap 3. A spend row absent from a date we never landed is missing data,
  -- not zero spend.
  (j.spend IS NULL AND c.metric_date IS NOT NULL)  AS is_genuine_zero_spend,
  (j.spend IS NULL AND c.metric_date IS NULL)      AS is_spend_data_missing,

  -- CPB, divided once. NULL rather than 0 or infinity when the denominator is
  -- empty, and suppressed entirely when we cannot vouch for the spend figure.
  CASE
    WHEN j.spend IS NULL AND c.metric_date IS NULL THEN NULL   -- broken pull
    WHEN j.bookings = 0                            THEN NULL   -- undefined
    ELSE ROUND(j.spend / CAST(j.bookings AS DOUBLE), 2)
  END                                              AS cpb,

  CASE
    WHEN j.spend IS NULL AND c.metric_date IS NULL      THEN NULL
    WHEN j.bookings_excl_canceled = 0                   THEN NULL
    ELSE ROUND(j.spend / CAST(j.bookings_excl_canceled AS DOUBLE), 2)
  END                                              AS cpb_excl_canceled,

  -- The stable figure. Daily CPB is noisy because attribution lags, so this is
  -- what a reader should trust for channel comparison. Window functions over
  -- the aggregate are safe: one row per (date, channel) by construction.
  ROUND(
    SUM(j.spend) OVER (
      PARTITION BY j.channel ORDER BY j.metric_date
      ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    )
    / NULLIF(CAST(SUM(j.bookings) OVER (
        PARTITION BY j.channel ORDER BY j.metric_date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
      ) AS DOUBLE), 0)
  , 2)                                             AS cpb_7d_rolling,

  DATE '{{asof}}'                                  AS build_date

FROM joined j
LEFT JOIN spend_coverage c ON c.metric_date = j.metric_date;
