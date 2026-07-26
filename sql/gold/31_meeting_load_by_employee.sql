-- =============================================================================
-- meeting_load_by_employee  ·  grain: host
-- =============================================================================
-- Average meetings per week per employee.
--
-- NUMERATOR IS PER-HOST, OFF THE BRIDGE. A co-hosted meeting is a unit of load
-- on EACH host, so a 3-host meeting is +1 to three people. This is exactly why
-- fct_booking_host exists, and exactly why per-employee load will NOT reconcile
-- with total meetings. That is correct, not a bug — but it must be said out
-- loud, because the totals visibly disagree.
--
-- ⚠️ DENOMINATOR IS TENURE WEEKS, NOT WINDOW WEEKS.
-- A rate's denominator must match the population of its numerator. Dividing
-- everyone by the reporting window punishes anyone not present throughout: a
-- new hire with 15 meetings in 2 weeks is doing 7.5/week, but against an 8-week
-- window shows 1.9 and looks like the LEAST loaded person when they are the
-- most.
--
-- ⚠️ TENURE WEEKS, NOT ACTIVE WEEKS.
-- Weeks with zero meetings must stay in the denominator. "Distinct weeks with
-- at least one meeting" structurally cannot reveal underload — an idle week
-- drops out, so an idle employee and a slammed one can show the same average.
-- Spotting the underloaded person is the entire point of a load metric.
--
-- ⚠️ THE DENOMINATOR IS A STATED PROXY.
-- True tenure needs a dim_employee with effective start and leave dates from an
-- HR roster. Calendly knows hosts, not HR. Absent that, tenure is proxied as
-- first-meeting-to-window-end, which misses pre-hire idle time and anyone who
-- has left. The proxy is labelled in the data rather than quietly substituted,
-- because a wrong-but-available number dressed as the right one is worse than
-- an honest approximation.
-- =============================================================================

CREATE TABLE insightflow_gold.meeting_load_by_employee__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{gold_bucket}}/meeting_load_by_employee/build_id={{build_id}}/'
) AS

WITH window_bounds AS (
  SELECT
    MIN(meeting_date_est) AS window_start,
    MAX(meeting_date_est) AS window_end
  FROM insightflow_silver.fct_booking_host
),

per_host AS (
  SELECT
    host_user_uri,
    MAX(host_name)                            AS host_name,
    MAX(host_email)                           AS host_email,
    COUNT(*)                                  AS total_meetings,
    COUNT(DISTINCT meeting_date_est)          AS days_with_meetings,
    COUNT(DISTINCT DATE_TRUNC('week', meeting_date_est)) AS active_weeks,
    MIN(meeting_date_est)                     AS first_meeting_date,
    MAX(meeting_date_est)                     AS last_meeting_date
  FROM insightflow_silver.fct_booking_host
  GROUP BY host_user_uri
),

tenure AS (
  SELECT
    p.*,
    w.window_start,
    w.window_end,
    -- The proxy: first meeting to window end, floored at one week so a host
    -- who started yesterday does not divide by zero and report an absurd rate.
    GREATEST(
      1.0,
      CAST(DATE_DIFF('day', p.first_meeting_date, w.window_end) AS DOUBLE) / 7.0
    )                                         AS tenure_weeks_proxy,
    CAST(DATE_DIFF('day', w.window_start, w.window_end) AS DOUBLE) / 7.0
                                              AS window_weeks
  FROM per_host p
  CROSS JOIN window_bounds w
)

SELECT
  host_user_uri,
  host_name,
  host_email,

  total_meetings,
  days_with_meetings,
  active_weeks,
  first_meeting_date,
  last_meeting_date,

  ROUND(tenure_weeks_proxy, 2)                AS tenure_weeks,
  ROUND(window_weeks, 2)                      AS window_weeks,

  -- THE metric. Tenure-weeks denominator, so idle weeks count against the
  -- average and underload is visible.
  ROUND(total_meetings / tenure_weeks_proxy, 2)        AS avg_meetings_per_week,

  -- Carried for contrast, deliberately. Comparing the two shows how much the
  -- naive denominator would have distorted this person's figure.
  ROUND(total_meetings / NULLIF(window_weeks, 0), 2)   AS avg_per_week_naive_window,
  ROUND(total_meetings / NULLIF(CAST(active_weeks AS DOUBLE), 0), 2)
                                                       AS avg_per_active_week,

  RANK() OVER (ORDER BY total_meetings / tenure_weeks_proxy DESC) AS rank_by_load,

  TRUE                                        AS tenure_is_proxied,
  'tenure proxied as first-meeting to window-end; true tenure needs an HR roster with effective dates, which Calendly does not provide'
                                              AS tenure_caveat,

  DATE '{{asof}}'                             AS build_date

FROM tenure;
