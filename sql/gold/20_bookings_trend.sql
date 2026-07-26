-- =============================================================================
-- bookings_trend  ·  grain: booking_date + channel
-- =============================================================================
-- Daily and weekly booking volume per source, with the smoothing a trend chart
-- needs to be readable.
--
-- A DENSE DATE SPINE. Days with no bookings are emitted as zero rows rather
-- than omitted. A line chart over sparse data draws a straight line across a
-- gap, which reads as "steady" when the truth is "nothing happened" — the same
-- absence-is-not-zero confusion as CPB, in visual form.
-- =============================================================================

CREATE TABLE insightflow_gold.bookings_trend__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{gold_bucket}}/bookings_trend/build_id={{build_id}}/'
) AS

WITH bounds AS (
  SELECT MIN(booking_date_est) AS min_date, MAX(booking_date_est) AS max_date
  FROM insightflow_silver.fct_booking
),

date_spine AS (
  SELECT CAST(d AS DATE) AS booking_date
  FROM bounds
  CROSS JOIN UNNEST(sequence(bounds.min_date, bounds.max_date, INTERVAL '1' DAY)) AS t(d)
),

channels AS (
  SELECT DISTINCT channel, is_paid_channel
  FROM insightflow_silver.fct_booking
),

actual AS (
  SELECT
    booking_date_est                          AS booking_date,
    channel,
    COUNT(*)                                  AS bookings,
    COUNT_IF(NOT is_canceled)                 AS bookings_excl_canceled
  FROM insightflow_silver.fct_booking
  GROUP BY booking_date_est, channel
),

dense AS (
  SELECT
    s.booking_date,
    c.channel,
    c.is_paid_channel,
    COALESCE(a.bookings, 0)                   AS bookings,
    COALESCE(a.bookings_excl_canceled, 0)     AS bookings_excl_canceled
  FROM date_spine s
  CROSS JOIN channels c
  LEFT JOIN actual a
    ON a.booking_date = s.booking_date AND a.channel = c.channel
)

SELECT
  booking_date,
  channel,
  is_paid_channel,
  bookings,
  bookings_excl_canceled,

  DATE_TRUNC('week', booking_date)            AS booking_week,

  SUM(bookings) OVER (
    PARTITION BY channel ORDER BY booking_date
    ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
  )                                           AS bookings_7d_rolling,

  SUM(bookings) OVER (
    PARTITION BY channel ORDER BY booking_date
  )                                           AS bookings_cumulative,

  DATE '{{asof}}'                             AS build_date

FROM dense;
