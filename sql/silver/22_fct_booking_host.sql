-- =============================================================================
-- fct_booking_host  ·  grain: scheduled_event_uri + user  ·  bridge
-- =============================================================================
-- event_memberships is an ARRAY, and an array field is a grain in disguise.
-- A meeting can have several hosts, so flattening one host onto fct_booking
-- would silently drop co-hosts and undercount meeting load per employee.
--
-- CONSEQUENCE TO STATE OUT LOUD: "total meetings" and "sum of meetings per
-- employee" will NOT reconcile. A 3-host meeting is one meeting and three units
-- of load. That is correct, not a bug, and it needs explaining before someone
-- notices the totals disagree and assumes the pipeline is broken.
-- =============================================================================

CREATE TABLE insightflow_silver.fct_booking_host__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{silver_bucket}}/fct_booking_host/build_id={{build_id}}/'
) AS

WITH bookings AS (
  SELECT
    json_extract_scalar(raw, '$.payload.scheduled_event.uri')            AS scheduled_event_uri,
    json_extract_scalar(raw, '$.payload.scheduled_event.start_time')     AS start_time_raw,
    json_extract_scalar(raw, '$.payload.created_at')                     AS created_at_raw,
    json_extract_scalar(raw, '$.payload.scheduled_event.event_type')     AS event_type,
    -- CAST to an array of rows so UNNEST can explode it. Members that do not
    -- match the shape become NULL rather than failing the build.
    TRY(CAST(
      json_extract(raw, '$.payload.scheduled_event.event_memberships')
      AS ARRAY(ROW(user VARCHAR, user_email VARCHAR, user_name VARCHAR))
    ))                                                                    AS memberships,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract_scalar(raw, '$.payload.scheduled_event.uri')
      ORDER BY json_extract_scalar(raw, '$.payload.created_at') ASC,
               json_extract_scalar(raw, '$.payload.uri') ASC
    ) AS rn
  FROM insightflow_bronze.calendly_booking
  WHERE json_extract_scalar(raw, '$.payload.scheduled_event.uri') IS NOT NULL
),

-- One row per meeting before exploding. Without this, a 3-invitee meeting with
-- 2 hosts would produce 6 bridge rows instead of 2 — the fan-out trap, one
-- level down.
one_row_per_meeting AS (
  SELECT * FROM bookings WHERE rn = 1
),

channel_map AS (
  SELECT
    json_extract_scalar(raw, '$.event_type') AS event_type,
    json_extract_scalar(raw, '$.channel')    AS channel
  FROM insightflow_bronze.seed_channel_map
)

SELECT
  b.scheduled_event_uri,
  m.user                                            AS host_user_uri,
  m.user_email                                      AS host_email,
  LOWER(TRIM(m.user_email))                         AS host_email_normalized,
  m.user_name                                       AS host_name,

  COALESCE(cm.channel, 'other')                     AS channel,

  TRY(from_iso8601_timestamp(b.start_time_raw))     AS start_time_utc,
  CAST(TRY(from_iso8601_timestamp(b.start_time_raw))
       AT TIME ZONE 'America/New_York' AS DATE)     AS meeting_date_est,
  CAST(TRY(from_iso8601_timestamp(b.created_at_raw))
       AT TIME ZONE 'America/New_York' AS DATE)     AS booking_date_est,

  DATE '{{asof}}'                                   AS build_date

FROM one_row_per_meeting b
CROSS JOIN UNNEST(b.memberships) AS t(m)
LEFT JOIN channel_map cm ON cm.event_type = b.event_type
WHERE m.user IS NOT NULL;
