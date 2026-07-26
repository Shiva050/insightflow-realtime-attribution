-- =============================================================================
-- video_booking_funnel  ·  grain: channel + booking_date
-- =============================================================================
-- The cross-source payoff: does video engagement lead to booked calls, and
-- through which channel?
--
-- BUILT BACKWARD, FROM BOOKINGS TO VIDEO.
-- Channel only exists at the booking stage — Wistia has no channel dimension.
-- And the video top is polluted by anonymity: most sessions carry no email, so
-- "all viewers" is unknowable and a forward conversion RATE is not honestly
-- computable. Every booking, by contrast, has a channel and an email, so
-- "of the bookings in channel X, how many had prior video engagement" has a
-- clean, fully-known denominator.
--
-- THE JOIN NEEDS IDENTITY AND A TEMPORAL GUARD.
-- Email alone would conflate every session that person ever had, including
-- watches AFTER they booked, which is backwards causally. A shared key is not a
-- cause. The window is bounded at 30 days because a watch 18 months before a
-- booking probably did not drive it — attribution is normally bounded, and
-- whether to bound it is a business call worth making explicitly.
--
-- COLLAPSE BEFORE JOINING. One booker with 40 sessions would otherwise inflate
-- the booking count 40x. DISTINCT on the booking key does that here.
--
-- ⚠️ THE TOUCH RATE IS A FLOOR, AND CURRENTLY THAT FLOOR IS ZERO.
-- No sampled Wistia event carried an email, so this metric currently reports 0
-- matched bookers. That means "we cannot measure whether video drove bookings",
-- NOT "video drove no bookings". The two are different claims and only the
-- coverage columns distinguish them — which is why they are materialised beside
-- every figure rather than left to a footnote.
-- =============================================================================

CREATE TABLE insightflow_gold.video_booking_funnel__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{gold_bucket}}/video_booking_funnel/build_id={{build_id}}/'
) AS

WITH bookers AS (
  -- One row per (meeting, identified invitee). The email is what crosses into
  -- the video side; bookings without one cannot participate in the match and
  -- are counted in the denominator anyway, because they are real bookings.
  SELECT
    b.scheduled_event_uri,
    b.channel,
    b.booking_date_est,
    b.created_at_utc,
    i.email_normalized
  FROM insightflow_silver.fct_booking b
  LEFT JOIN insightflow_silver.fct_invitee i
    ON i.scheduled_event_uri = b.scheduled_event_uri
),

-- DISTINCT collapses the video side to the booking grain at join time, so a
-- heavy watcher cannot fan out the booking count.
matched AS (
  SELECT DISTINCT
    bk.scheduled_event_uri,
    bk.channel,
    bk.booking_date_est
  FROM bookers bk
  JOIN insightflow_silver.fct_visitor_event v
    ON  v.email_normalized = bk.email_normalized
    AND v.received_at_utc <  bk.created_at_utc                          -- prior
    AND v.received_at_utc >= bk.created_at_utc - INTERVAL '30' DAY      -- bounded
  WHERE bk.email_normalized IS NOT NULL
),

-- People and sessions are two different measures and must never be conflated
-- into one ambiguous "total video engagement".
intensity AS (
  SELECT
    bk.scheduled_event_uri,
    COUNT(*)                                  AS prior_sessions,
    AVG(v.percent_viewed)                     AS avg_percent_viewed,
    MAX(v.percent_viewed)                     AS max_percent_viewed
  FROM bookers bk
  JOIN insightflow_silver.fct_visitor_event v
    ON  v.email_normalized = bk.email_normalized
    AND v.received_at_utc <  bk.created_at_utc
    AND v.received_at_utc >= bk.created_at_utc - INTERVAL '30' DAY
  WHERE bk.email_normalized IS NOT NULL
  GROUP BY bk.scheduled_event_uri
),

bookings_agg AS (
  SELECT
    b.channel,
    b.booking_date_est,
    COUNT(DISTINCT b.scheduled_event_uri)                       AS bookings,
    COUNT(DISTINCT CASE WHEN i.email_normalized IS NOT NULL
                        THEN b.scheduled_event_uri END)         AS bookings_with_email
  FROM insightflow_silver.fct_booking b
  LEFT JOIN insightflow_silver.fct_invitee i
    ON i.scheduled_event_uri = b.scheduled_event_uri
  GROUP BY b.channel, b.booking_date_est
),

matched_agg AS (
  SELECT
    channel,
    booking_date_est,
    COUNT(DISTINCT scheduled_event_uri)       AS bookings_with_prior_video
  FROM matched
  GROUP BY channel, booking_date_est
),

intensity_agg AS (
  SELECT
    m.channel,
    m.booking_date_est,
    SUM(i.prior_sessions)                     AS prior_sessions,
    AVG(i.avg_percent_viewed)                 AS avg_percent_viewed
  FROM matched m
  JOIN intensity i ON i.scheduled_event_uri = m.scheduled_event_uri
  GROUP BY m.channel, m.booking_date_est
),

-- Identification rate on the video side, the number that explains a zero touch
-- rate. Reported for the whole feed rather than per channel, because Wistia
-- events carry no channel.
video_coverage AS (
  SELECT
    COUNT(*)                                  AS total_sessions,
    COUNT_IF(is_identified)                   AS identified_sessions
  FROM insightflow_silver.fct_visitor_event
)

SELECT
  b.channel,
  b.booking_date_est                          AS booking_date,

  b.bookings,
  b.bookings_with_email,
  COALESCE(m.bookings_with_prior_video, 0)    AS bookings_with_prior_video,

  -- A FLOOR, not an exact rate: anonymous watchers cannot join, so matched
  -- video systematically undercounts true video influence.
  CASE WHEN b.bookings > 0
       THEN ROUND(CAST(COALESCE(m.bookings_with_prior_video, 0) AS DOUBLE)
                  / b.bookings, 4)
  END                                         AS video_touch_rate_floor,

  -- People vs sessions, kept separate.
  COALESCE(i.prior_sessions, 0)               AS prior_video_sessions,
  ROUND(i.avg_percent_viewed, 4)              AS avg_percent_viewed,

  -- Coverage compounds along the chain, so every rate that gates the funnel
  -- travels with it. A reader who sees touch_rate = 0 alongside
  -- identification_rate = 0 knows which claim is supported.
  CASE WHEN b.bookings > 0
       THEN ROUND(CAST(b.bookings_with_email AS DOUBLE) / b.bookings, 4)
  END                                         AS booking_email_coverage,
  CASE WHEN vc.total_sessions > 0
       THEN ROUND(CAST(vc.identified_sessions AS DOUBLE) / vc.total_sessions, 4)
  END                                         AS video_identification_rate,

  CASE
    WHEN vc.identified_sessions = 0 THEN
      'NOT MEASURABLE: no video session carries an email, so the bridge cannot join. This is not evidence that video drove no bookings.'
    ELSE
      'LOWER BOUND: anonymous watchers cannot join, so true video influence is at least this.'
  END                                         AS interpretation,

  DATE '{{asof}}'                             AS build_date

FROM bookings_agg b
LEFT JOIN matched_agg   m  ON m.channel = b.channel
                          AND m.booking_date_est = b.booking_date_est
LEFT JOIN intensity_agg i  ON i.channel = b.channel
                          AND i.booking_date_est = b.booking_date_est
CROSS JOIN video_coverage vc;
