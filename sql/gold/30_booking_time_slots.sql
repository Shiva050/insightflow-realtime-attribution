-- =============================================================================
-- booking_time_slots  ·  grain: hour_of_day + day_of_week + channel + perspective
-- =============================================================================
-- "When do people book" is TWO metrics wearing one name, and a single timezone
-- cannot serve both:
--
--   CUSTOMER PREFERENCE — the invitee's local hour. A noon PST call and a 4pm
--   CST call are both "early afternoon local", which is what ad scheduling and
--   creative timing care about.
--
--   STAFFING DEMAND — the business hour in EST. Those same two calls hit the
--   team at different physical moments, which is what resource planning needs.
--
-- Truncating a bare UTC hour answers NOBODY's question. Both perspectives are
-- emitted as rows of one table with a `perspective` column, so the dashboard
-- toggles between them rather than silently serving one and misleading the
-- other consumer.
--
-- start_time, not created_at: this is about when the meeting happens.
-- created_at is the acquisition signal and already drives CPB.
--
-- Rows whose invitee timezone is null cannot feed the customer-preference view.
-- They are excluded from it and counted, rather than defaulted to EST, which
-- would quietly relabel someone else's morning as ours.
-- =============================================================================

CREATE TABLE insightflow_gold.booking_time_slots__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{gold_bucket}}/booking_time_slots/build_id={{build_id}}/'
) AS

WITH customer_preference AS (
  SELECT
    'customer_local'                          AS perspective,
    start_hour_invitee_local                  AS hour_of_day,
    start_dow_invitee_local                   AS day_of_week,
    channel,
    COUNT(*)                                  AS bookings,
    COUNT_IF(NOT is_canceled)                 AS bookings_excl_canceled
  FROM insightflow_silver.fct_booking
  WHERE NOT missing_invitee_timezone
    AND start_hour_invitee_local IS NOT NULL
  GROUP BY start_hour_invitee_local, start_dow_invitee_local, channel
),

staffing_demand AS (
  SELECT
    'business_est'                            AS perspective,
    start_hour_business_est                   AS hour_of_day,
    start_dow_business_est                    AS day_of_week,
    channel,
    COUNT(*)                                  AS bookings,
    COUNT_IF(NOT is_canceled)                 AS bookings_excl_canceled
  FROM insightflow_silver.fct_booking
  WHERE start_hour_business_est IS NOT NULL
  GROUP BY start_hour_business_est, start_dow_business_est, channel
),

combined AS (
  SELECT * FROM customer_preference
  UNION ALL
  SELECT * FROM staffing_demand
),

-- How much of the data each perspective can actually describe. The customer
-- view is built on a subset, and that needs saying next to the heatmap.
coverage AS (
  SELECT
    COUNT(*)                                  AS total_bookings,
    COUNT_IF(missing_invitee_timezone)        AS bookings_missing_timezone
  FROM insightflow_silver.fct_booking
)

SELECT
  c.perspective,
  c.hour_of_day,
  c.day_of_week,
  c.channel,
  c.bookings,
  c.bookings_excl_canceled,

  -- Share within its own perspective, so the two heatmaps are each internally
  -- normalised and not accidentally compared cell-to-cell.
  ROUND(CAST(c.bookings AS DOUBLE)
        / NULLIF(SUM(c.bookings) OVER (PARTITION BY c.perspective), 0), 4)
                                              AS share_of_perspective,

  CASE WHEN c.perspective = 'customer_local'
       THEN ROUND(CAST(cv.total_bookings - cv.bookings_missing_timezone AS DOUBLE)
                  / NULLIF(cv.total_bookings, 0), 4)
       ELSE 1.0
  END                                         AS perspective_coverage_rate,

  DATE '{{asof}}'                             AS build_date

FROM combined c
CROSS JOIN coverage cv;
