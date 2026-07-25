-- =============================================================================
-- dim_media  ·  grain: hashed_id  ·  SCD Type 1
-- =============================================================================
-- Video metadata. Feeds media-quality metrics only.
--
-- This dimension deliberately cannot reach the CRM bridge. Media-grained
-- endpoints carry no visitor_key and no email, so no amount of massaging gets
-- from here to a lead — when an endpoint's grain does not contain the key your
-- model needs, it is the wrong endpoint. The bridge lives in fct_visitor_event.
--
-- Bronze keeps a daily snapshot per media. Only the latest is materialised
-- here, but the snapshots remain, so a Type 2 is available later without
-- re-ingesting.
-- =============================================================================

CREATE TABLE insightflow_silver.dim_media__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{silver_bucket}}/dim_media/build_id={{build_id}}/'
) AS

WITH snapshots AS (
  SELECT
    json_extract_scalar(raw, '$.hashed_id')      AS hashed_id,
    json_extract_scalar(raw, '$.name')           AS media_name,
    json_extract_scalar(raw, '$.type')           AS media_type,
    json_extract_scalar(raw, '$.created')        AS created_at_raw,
    json_extract_scalar(raw, '$.updated')        AS updated_at_raw,
    TRY(CAST(json_extract_scalar(raw, '$.duration') AS DOUBLE))   AS duration_seconds,
    json_extract_scalar(raw, '$.description')    AS description,
    asof                                         AS asof_date,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract_scalar(raw, '$.hashed_id')
      ORDER BY asof DESC
    ) AS rn
  FROM insightflow_bronze.wistia_media
  WHERE json_extract_scalar(raw, '$.hashed_id') IS NOT NULL
)

SELECT
  hashed_id,
  media_name,
  media_type,
  duration_seconds,
  ROUND(duration_seconds / 60.0, 2)                AS duration_minutes,
  description,
  TRY(from_iso8601_timestamp(created_at_raw))      AS media_created_at,
  TRY(from_iso8601_timestamp(updated_at_raw))      AS media_updated_at,
  asof_date                                        AS sourced_from_asof,
  DATE '{{asof}}'                                  AS build_date
FROM snapshots
WHERE rn = 1;
