-- =============================================================================
-- fct_media_daily_stats  ·  grain: hashed_id + date  ·  FLOW
-- =============================================================================
-- Straight from the by_date endpoint, which hands us the daily delta directly.
-- Because the source is a flow rather than a cumulative counter, none of the
-- snapshot-differencing apparatus is needed: no LAG, no days_covered flag, no
-- first-snapshot baseline, no negative clamping. Verifying the endpoint shape
-- before designing removed all of it.
--
-- ⚠️ NO play_rate COLUMN, DELIBERATELY.
--
-- A ratio is non-additive. It cannot be summed, and averaging daily rates
-- weights a 3-load day the same as a 3,000-load day. The live data shows how
-- badly that misleads: over 31 days one media ran 6455 loads / 48 plays
-- (0.74%) and the other 44 loads / 18 plays (40.91%). Averaging the two rates
-- gives ~21%; the correct volume-weighted figure is 66/6499 = 1.02%. A 20x
-- error that looks entirely plausible on a dashboard.
--
-- So the additive components travel to the target grain and Gold divides ONCE.
-- =============================================================================

CREATE TABLE insightflow_silver.fct_media_daily_stats__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{silver_bucket}}/fct_media_daily_stats/build_id={{build_id}}/'
) AS

WITH parsed AS (
  SELECT
    -- The file is one object per (media, day); hashed_id comes from the object
    -- key's partition sibling, so it is carried in the payload path instead.
    regexp_extract("$path", '/([^/]+)\.json$', 1)                     AS hashed_id,
    json_extract_scalar(raw, '$.date')                                AS stat_date_str,
    TRY(CAST(json_extract_scalar(raw, '$.load_count')    AS BIGINT))  AS load_count,
    TRY(CAST(json_extract_scalar(raw, '$.play_count')    AS BIGINT))  AS play_count,
    TRY(CAST(json_extract_scalar(raw, '$.hours_watched') AS DOUBLE))  AS hours_watched,
    dt                                                                AS partition_dt,
    ROW_NUMBER() OVER (
      PARTITION BY regexp_extract("$path", '/([^/]+)\.json$', 1),
                   json_extract_scalar(raw, '$.date')
      ORDER BY dt DESC
    ) AS rn
  FROM insightflow_bronze.wistia_media_stats
  WHERE json_extract_scalar(raw, '$.date') IS NOT NULL
)

SELECT
  hashed_id,
  CAST(stat_date_str AS DATE)                      AS stat_date,

  -- Additive components only. Divide last, at whatever grain the metric needs.
  load_count,
  play_count,
  hours_watched,

  -- A zero-load day is a real observation, not missing data — the API returns
  -- explicit zero rows for quiet days, so absence here means we never asked.
  (load_count = 0 AND play_count = 0)              AS is_zero_activity_day,

  DATE '{{asof}}'                                  AS build_date

FROM parsed
WHERE rn = 1;
