-- =============================================================================
-- fct_invitee  ·  grain: invitee_uri  ·  one person on a booking
-- =============================================================================
-- The invitee-grained companion to fct_booking. Holds what only exists per
-- person: email, name, timezone, and the booking questionnaire.
--
-- THIS IS WHERE THE CRM BRIDGE LIVES. email_normalized here joins to
-- dim_lead.lead_email_normalized, which is the hop from a booking to a lead.
--
-- Kept separate from fct_booking rather than folded in, because a meeting can
-- have several invitees and flattening them onto the meeting row would either
-- drop people or fan the meeting out.
-- =============================================================================

CREATE TABLE insightflow_silver.fct_invitee__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{silver_bucket}}/fct_invitee/build_id={{build_id}}/'
) AS

WITH invitees AS (
  SELECT
    json_extract_scalar(raw, '$.payload.uri')                        AS invitee_uri,
    json_extract_scalar(raw, '$.payload.scheduled_event.uri')        AS scheduled_event_uri,
    json_extract_scalar(raw, '$.payload.email')                      AS email,
    json_extract_scalar(raw, '$.payload.name')                       AS invitee_name,
    json_extract_scalar(raw, '$.payload.first_name')                 AS first_name,
    json_extract_scalar(raw, '$.payload.last_name')                  AS last_name,
    json_extract_scalar(raw, '$.payload.timezone')                   AS invitee_timezone,
    json_extract_scalar(raw, '$.payload.status')                     AS invitee_status,
    json_extract_scalar(raw, '$.payload.created_at')                 AS created_at_raw,
    json_extract_scalar(raw, '$.payload.updated_at')                 AS updated_at_raw,
    json_extract_scalar(raw, '$.payload.rescheduled')                AS rescheduled_raw,
    json_extract(raw, '$.payload.questions_and_answers')             AS questions_and_answers,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract_scalar(raw, '$.payload.uri')
      ORDER BY TRY(from_iso8601_timestamp(
                 json_extract_scalar(raw, '$.payload.updated_at'))) DESC NULLS LAST
    ) AS rn
  FROM insightflow_bronze.calendly_booking
  WHERE json_extract_scalar(raw, '$.payload.uri') IS NOT NULL
),

cancellations AS (
  SELECT
    json_extract_scalar(raw, '$.payload.uri')                        AS invitee_uri,
    json_extract_scalar(raw, '$.payload.cancellation.reason')        AS cancel_reason,
    json_extract_scalar(raw, '$.payload.cancellation.canceled_by')   AS canceled_by,
    COALESCE(
      json_extract_scalar(raw, '$.payload.cancellation.created_at'),
      json_extract_scalar(raw, '$.payload.updated_at')
    )                                                                AS canceled_at_raw,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract_scalar(raw, '$.payload.uri')
      ORDER BY json_extract_scalar(raw, '$.payload.updated_at') DESC
    ) AS rn
  FROM insightflow_bronze.calendly_cancellation
  WHERE json_extract_scalar(raw, '$.payload.uri') IS NOT NULL
)

SELECT
  i.invitee_uri,
  i.scheduled_event_uri,

  i.email,
  -- Normalised once, in Silver, on both sides of the bridge. The raw form stays
  -- for debugging and so a rule change can be recomputed.
  LOWER(TRIM(i.email))                              AS email_normalized,
  (i.email IS NOT NULL AND TRIM(i.email) <> '')     AS has_email,

  i.invitee_name,
  i.first_name,
  i.last_name,
  i.invitee_timezone,
  i.invitee_status,

  CAST(TRY(from_iso8601_timestamp(i.created_at_raw)) AS TIMESTAMP)     AS created_at_utc,
  CAST(at_timezone(TRY(from_iso8601_timestamp(i.created_at_raw)), 'America/New_York') AS DATE)     AS booking_date_est,
  CAST(TRY(from_iso8601_timestamp(i.updated_at_raw)) AS TIMESTAMP)     AS updated_at_utc,

  COALESCE(TRY(CAST(i.rescheduled_raw AS BOOLEAN)), FALSE) AS is_rescheduled,

  (c.invitee_uri IS NOT NULL)                       AS is_canceled,
  CAST(TRY(from_iso8601_timestamp(c.canceled_at_raw)) AS TIMESTAMP)    AS canceled_at,
  c.cancel_reason,
  c.canceled_by,

  -- Kept as JSON rather than exploded: the questionnaire is free-form and its
  -- shape varies per event type, so flattening it here would bake in today's
  -- questions.
  json_format(i.questions_and_answers)              AS questions_and_answers_json,

  DATE '{{asof}}'                                   AS build_date

FROM invitees i
LEFT JOIN cancellations c
  ON c.invitee_uri = i.invitee_uri AND c.rn = 1
WHERE i.rn = 1;
