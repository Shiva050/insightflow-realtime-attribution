-- =============================================================================
-- fct_visitor_event  ·  grain: event_key  ·  FLOW  ·  THE VIDEO→LEAD BRIDGE
-- =============================================================================
-- One viewing session. Because the events endpoint exists, visitor activity is
-- a flow and activity-over-time is derivable by grouping this stream — no daily
-- snapshots, no LAG, no seed-row baseline, no negative clamping. Verifying that
-- the endpoint existed collapsed three tables of snapshot machinery into this
-- one fact.
--
-- The row carries BOTH visitor_key and media_id, so it doubles as the
-- visitor↔media link, and it carries email, which is the hop to dim_lead.
--
-- ⚠️ COLLAPSE TO email BEFORE JOINING, NOT TO visitor_key.
-- Two devices share one email, so aggregating per visitor and then joining on
-- email still fans out. The pre-join collapse must be to the grain of the JOIN
-- KEY. And the join needs a temporal guard as well as identity — a shared key
-- is not a cause, so attribution requires received_at < booking.created_at.
-- Both belong in Gold; Silver just makes them possible.
--
-- ⚠️ THE IDENTIFICATION RATE IS CURRENTLY ZERO.
-- No sampled event carried an email. The bridge is structurally correct and
-- currently joins nothing. is_identified is materialised here so that fact is
-- visible in the data rather than inferred from an empty result — "we cannot
-- measure whether video drove bookings" and "video drove no bookings" are
-- different claims.
-- =============================================================================

CREATE TABLE insightflow_silver.fct_visitor_event__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{silver_bucket}}/fct_visitor_event/build_id={{build_id}}/'
) AS

WITH parsed AS (
  SELECT
    json_extract_scalar(raw, '$.event_key')                      AS event_key,
    json_extract_scalar(raw, '$.visitor_key')                    AS visitor_key,
    json_extract_scalar(raw, '$.media_id')                       AS hashed_id,
    json_extract_scalar(raw, '$.media_name')                     AS media_name,
    json_extract_scalar(raw, '$.email')                          AS email,
    json_extract_scalar(raw, '$.received_at')                    AS received_at_raw,
    TRY(CAST(json_extract_scalar(raw, '$.percent_viewed') AS DOUBLE)) AS percent_viewed,
    json_extract_scalar(raw, '$.country')                        AS country,
    json_extract_scalar(raw, '$.region')                         AS region,
    json_extract_scalar(raw, '$.city')                           AS city,
    json_extract_scalar(raw, '$.org')                            AS org,
    json_extract_scalar(raw, '$.conversion_type')                AS conversion_type,
    json_extract_scalar(raw, '$.embed_url')                      AS embed_url,
    dt                                                           AS partition_dt,
    -- The same event can be re-pulled across overlapping day windows and
    -- backfills. event_key is the natural identity, so the newest partition
    -- wins and the duplicate collapses.
    ROW_NUMBER() OVER (
      PARTITION BY json_extract_scalar(raw, '$.event_key')
      ORDER BY dt DESC
    ) AS rn
  FROM insightflow_bronze.wistia_visitor_event
  WHERE json_extract_scalar(raw, '$.event_key') IS NOT NULL
)

SELECT
  event_key,
  visitor_key,
  hashed_id,
  media_name,

  email,
  LOWER(TRIM(email))                               AS email_normalized,
  (email IS NOT NULL AND TRIM(email) <> '')        AS is_identified,

  CAST(TRY(from_iso8601_timestamp(received_at_raw)) AS TIMESTAMP)     AS received_at_utc,
  CAST(at_timezone(TRY(from_iso8601_timestamp(received_at_raw)), 'America/New_York') AS DATE)    AS event_date_est,

  percent_viewed,
  -- A load with no meaningful watch is not engagement. Kept as a flag rather
  -- than a filter, so each metric decides its own threshold.
  (percent_viewed >= 0.25)                         AS watched_quarter,
  (percent_viewed >= 0.50)                         AS watched_half,

  country,
  region,
  city,
  org,
  conversion_type,
  embed_url,

  DATE '{{asof}}'                                  AS build_date

FROM parsed
WHERE rn = 1;
