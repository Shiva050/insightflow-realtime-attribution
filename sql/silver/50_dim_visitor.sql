-- =============================================================================
-- dim_visitor  ·  grain: visitor_key  ·  SCD Type 1
-- =============================================================================
-- Conformed visitor identity: email, org, geography, user agent.
--
-- IDENTITY ONLY. Activity counts live in fct_visitor_event. A visitor row mixes
-- never-changing identity with counts that move on every watch, and
-- snapshotting the whole row daily would restate the email 365 times a year to
-- track two moving numbers. Descriptive attributes belong in the dimension,
-- measures in the fact.
--
-- ⚠️ visitor_key IS A DEVICE, NOT A PERSON.
-- One human on a phone and a laptop is two visitor_keys with one email. The
-- business counts people, so anything crossing into the funnel collapses to
-- email — see the note in fct_visitor_event.
--
-- Derived from the events stream rather than the visitors endpoint: every
-- identity attribute we need is already on the event row, so a second feed
-- would add a source without adding information.
-- =============================================================================

CREATE TABLE insightflow_silver.dim_visitor__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{silver_bucket}}/dim_visitor/build_id={{build_id}}/'
) AS

WITH events AS (
  SELECT
    json_extract_scalar(raw, '$.visitor_key')                    AS visitor_key,
    json_extract_scalar(raw, '$.email')                          AS email,
    json_extract_scalar(raw, '$.org')                            AS org,
    json_extract_scalar(raw, '$.country')                        AS country,
    json_extract_scalar(raw, '$.region')                         AS region,
    json_extract_scalar(raw, '$.city')                           AS city,
    json_extract_scalar(raw, '$.ip')                             AS ip,
    json_extract_scalar(raw, '$.user_agent_details.browser')     AS browser,
    json_extract_scalar(raw, '$.user_agent_details.platform')    AS platform,
    json_extract_scalar(raw, '$.user_agent_details.mobile')      AS is_mobile_raw,
    json_extract_scalar(raw, '$.received_at')                    AS received_at_raw
  FROM insightflow_bronze.wistia_visitor_event
  WHERE json_extract_scalar(raw, '$.visitor_key') IS NOT NULL
),

-- An email can appear on a later event than the first sighting, so identity is
-- taken from the most recent event that HAS one, falling back to the most
-- recent event overall. Taking the latest event blindly would discard a known
-- email the moment the same device watched anonymously again.
identity AS (
  SELECT
    visitor_key,
    MAX_BY(email,    CASE WHEN email IS NOT NULL
                          THEN TRY(from_iso8601_timestamp(received_at_raw)) END) AS email,
    MAX_BY(org,      TRY(from_iso8601_timestamp(received_at_raw)))               AS org,
    MAX_BY(country,  TRY(from_iso8601_timestamp(received_at_raw)))               AS country,
    MAX_BY(region,   TRY(from_iso8601_timestamp(received_at_raw)))               AS region,
    MAX_BY(city,     TRY(from_iso8601_timestamp(received_at_raw)))               AS city,
    MAX_BY(ip,       TRY(from_iso8601_timestamp(received_at_raw)))               AS ip,
    MAX_BY(browser,  TRY(from_iso8601_timestamp(received_at_raw)))               AS browser,
    MAX_BY(platform, TRY(from_iso8601_timestamp(received_at_raw)))               AS platform,
    MAX_BY(is_mobile_raw, TRY(from_iso8601_timestamp(received_at_raw)))          AS is_mobile_raw,
    MIN(TRY(from_iso8601_timestamp(received_at_raw)))                            AS first_seen_at,
    MAX(TRY(from_iso8601_timestamp(received_at_raw)))                            AS last_seen_at
  FROM events
  GROUP BY visitor_key
)

SELECT
  visitor_key,

  email,
  -- Both sides of the bridge normalise identically, or the join silently misses.
  LOWER(TRIM(email))                               AS email_normalized,
  -- Anonymous viewing is a fact to report, not a defect to clean away. A null
  -- visitor email is expected; a null LEAD email is a data-quality problem.
  -- Same null, different meaning, because the grain's relationship to "person"
  -- differs.
  (email IS NOT NULL AND TRIM(email) <> '')        AS is_identified,

  org,
  country,
  region,
  city,
  ip,
  browser,
  platform,
  TRY(CAST(is_mobile_raw AS BOOLEAN))              AS is_mobile,

  first_seen_at,
  last_seen_at,

  DATE '{{asof}}'                                  AS build_date

FROM identity;
