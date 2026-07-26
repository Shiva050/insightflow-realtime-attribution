-- =============================================================================
-- daily_calls_by_source  ·  grain: booking_date + channel
-- =============================================================================
-- Count of bookings per source per day.
--
-- A plain COUNT, not COUNT(DISTINCT scheduled_event_uri) — because fct_booking
-- is already at meeting grain. That is the payoff of splitting bookings and
-- invitees into separate Silver tables: the DISTINCT is done once, in the
-- model, instead of being remembered in every metric that touches it.
--
-- ALL sources, not just the paid three. The webhook subscription is org-wide
-- and Silver tagged every booking with a channel, so this metric takes them all
-- while CPB filters to channels where spend exists. Silver tags, Gold selects.
-- =============================================================================

CREATE TABLE insightflow_gold.daily_calls_by_source__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{gold_bucket}}/daily_calls_by_source/build_id={{build_id}}/'
) AS

SELECT
  booking_date_est                            AS booking_date,
  channel,
  is_paid_channel,

  COUNT(*)                                    AS bookings,
  COUNT_IF(NOT is_canceled)                   AS bookings_excl_canceled,
  COUNT_IF(is_canceled)                       AS bookings_canceled,
  SUM(invitee_count)                          AS invitees,

  -- Carried so a reader can see when the two diverge: a meeting with four
  -- invitees is still one booking, and CPB depends on that.
  ROUND(CAST(SUM(invitee_count) AS DOUBLE) / NULLIF(COUNT(*), 0), 2)
                                              AS avg_invitees_per_booking,

  DATE '{{asof}}'                             AS build_date

FROM insightflow_silver.fct_booking
GROUP BY booking_date_est, channel, is_paid_channel;
