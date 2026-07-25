-- =============================================================================
-- fct_booking  ·  grain: scheduled_event_uri  ·  one booked MEETING
-- =============================================================================
-- ONE TABLE PER GRAIN. One webhook fires per invitee, so a 3-invitee meeting
-- lands 3 Bronze objects carrying the same scheduled_event. Five of the six
-- Calendly metrics are meeting-grained, so if this table stayed at invitee
-- grain every one of them would need COUNT(DISTINCT scheduled_event_uri) —
-- grain repair at query time, and a silent double-count everywhere it is
-- forgotten. Collapsing here makes those metrics a plain COUNT.
--
-- SILVER TAGS, GOLD SELECTS. The subscription is org-wide, so non-campaign
-- bookings arrive too. Every booking is tagged with a channel and kept; each
-- Gold metric applies its own filter. CPB wants the three paid channels,
-- Daily Calls Booked by Source wants all of them.
--
-- TIMEZONE. Both hour grains are derived here rather than in Gold, because
-- "when do people book" is two questions: customer preference in the invitee's
-- local hour, and staffing demand in the business hour. One timezone cannot
-- serve both, and a bare UTC hour answers neither.
-- =============================================================================

CREATE TABLE insightflow_silver.fct_booking__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{silver_bucket}}/fct_booking/build_id={{build_id}}/'
) AS

WITH invitee_events AS (
  SELECT
    json_extract_scalar(raw, '$.payload.scheduled_event.uri')          AS scheduled_event_uri,
    json_extract_scalar(raw, '$.payload.uri')                          AS invitee_uri,
    json_extract_scalar(raw, '$.payload.created_at')                   AS invitee_created_at_raw,
    json_extract_scalar(raw, '$.payload.timezone')                     AS invitee_timezone,
    json_extract_scalar(raw, '$.payload.scheduled_event.created_at')   AS meeting_created_at_raw,
    json_extract_scalar(raw, '$.payload.scheduled_event.start_time')   AS start_time_raw,
    json_extract_scalar(raw, '$.payload.scheduled_event.end_time')     AS end_time_raw,
    json_extract_scalar(raw, '$.payload.scheduled_event.name')         AS meeting_name,
    json_extract_scalar(raw, '$.payload.scheduled_event.status')       AS meeting_status,
    json_extract_scalar(raw, '$.payload.scheduled_event.event_type')   AS event_type,
    json_extract_scalar(raw, '$.payload.tracking.utm_source')          AS utm_source,
    json_extract_scalar(raw, '$.payload.tracking.utm_campaign')        AS utm_campaign,
    json_extract_scalar(raw, '$.payload.tracking.utm_medium')          AS utm_medium
  FROM insightflow_bronze.calendly_booking
  WHERE json_extract_scalar(raw, '$.payload.scheduled_event.uri') IS NOT NULL
),

-- Collapse invitee rows to one row per meeting. Ordering is deterministic so
-- the build is reproducible; the meeting-level attributes are identical across
-- a meeting's invitees, so which row wins does not change any value.
meetings AS (
  SELECT *
  FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (
        PARTITION BY scheduled_event_uri
        ORDER BY TRY(from_iso8601_timestamp(invitee_created_at_raw)) ASC NULLS LAST,
                 invitee_uri ASC
      ) AS rn
    FROM invitee_events
  )
  WHERE rn = 1
),

-- Cancellations arrive as their own webhook under their own prefix, keyed on
-- the invitee. A meeting counts as cancelled if any of its invitees cancelled.
cancellations AS (
  SELECT
    json_extract_scalar(raw, '$.payload.scheduled_event.uri')          AS scheduled_event_uri,
    MIN(COALESCE(
      json_extract_scalar(raw, '$.payload.cancellation.created_at'),
      json_extract_scalar(raw, '$.payload.updated_at')
    ))                                                                 AS canceled_at_raw
  FROM insightflow_bronze.calendly_cancellation
  WHERE json_extract_scalar(raw, '$.payload.scheduled_event.uri') IS NOT NULL
  GROUP BY json_extract_scalar(raw, '$.payload.scheduled_event.uri')
),

channel_map AS (
  SELECT
    json_extract_scalar(raw, '$.event_type')                AS event_type,
    json_extract_scalar(raw, '$.channel')                   AS channel,
    CAST(json_extract_scalar(raw, '$.is_paid') AS BOOLEAN)  AS is_paid
  FROM insightflow_bronze.seed_channel_map
),

invitee_counts AS (
  SELECT scheduled_event_uri, COUNT(DISTINCT invitee_uri) AS invitee_count
  FROM invitee_events
  GROUP BY scheduled_event_uri
)

SELECT
  m.scheduled_event_uri,
  m.meeting_name,
  m.meeting_status,
  m.event_type,

  -- Unmapped event types are tagged 'other', never dropped: the subscription is
  -- org-wide and the all-sources metric needs them.
  COALESCE(cm.channel, 'other')                     AS channel,
  COALESCE(cm.is_paid, FALSE)                       AS is_paid_channel,

  -- ATTRIBUTION DATE. created_at, not start_time: CPB asks what the dollars
  -- spent on day X bought, so spend and booking-creation are the cause-effect
  -- pair. start_time is when the call happens, which would smear today's spend
  -- across future meeting dates.
  TRY(from_iso8601_timestamp(m.invitee_created_at_raw))                AS created_at_utc,
  -- Normalised to EST BEFORE truncating to a date, or midnight-boundary
  -- bookings land on the wrong day and quietly misattribute spend.
  CAST(TRY(from_iso8601_timestamp(m.invitee_created_at_raw))
       AT TIME ZONE 'America/New_York' AS DATE)                        AS booking_date_est,

  TRY(from_iso8601_timestamp(m.start_time_raw))                        AS start_time_utc,
  TRY(from_iso8601_timestamp(m.end_time_raw))                          AS end_time_utc,

  -- Two hour grains for two different questions (see header).
  m.invitee_timezone,
  CASE WHEN m.invitee_timezone IS NOT NULL
       THEN HOUR(TRY(from_iso8601_timestamp(m.start_time_raw))
                 AT TIME ZONE m.invitee_timezone)
  END                                               AS start_hour_invitee_local,
  CASE WHEN m.invitee_timezone IS NOT NULL
       THEN DAY_OF_WEEK(TRY(from_iso8601_timestamp(m.start_time_raw))
                        AT TIME ZONE m.invitee_timezone)
  END                                               AS start_dow_invitee_local,
  HOUR(TRY(from_iso8601_timestamp(m.start_time_raw))
       AT TIME ZONE 'America/New_York')             AS start_hour_business_est,
  DAY_OF_WEEK(TRY(from_iso8601_timestamp(m.start_time_raw))
              AT TIME ZONE 'America/New_York')      AS start_dow_business_est,
  -- Rows without a timezone cannot feed the customer-preference heatmap and
  -- belong in a coverage bucket rather than being silently defaulted to EST.
  (m.invitee_timezone IS NULL)                      AS missing_invitee_timezone,

  ic.invitee_count,

  (c.scheduled_event_uri IS NOT NULL)               AS is_canceled,
  TRY(from_iso8601_timestamp(c.canceled_at_raw))    AS canceled_at,

  -- The spec asks for UTM-based attribution, but every UTM field was null in
  -- the source sample, which is why channel comes from the event_type map.
  -- Carried anyway so the assumption stays checkable rather than assumed.
  m.utm_source,
  m.utm_campaign,
  m.utm_medium,

  DATE '{{asof}}'                                   AS build_date

FROM meetings m
LEFT JOIN channel_map    cm ON cm.event_type = m.event_type
LEFT JOIN cancellations  c  ON c.scheduled_event_uri = m.scheduled_event_uri
LEFT JOIN invitee_counts ic ON ic.scheduled_event_uri = m.scheduled_event_uri;
