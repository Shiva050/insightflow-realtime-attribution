-- =============================================================================
-- media_engagement  ·  grain: hashed_id + stat_date
-- =============================================================================
-- Video quality metrics: loads, plays, hours watched, and play rate.
--
-- ⚠️ play_rate IS COMPUTED HERE, ONCE, FROM SUMMED COMPONENTS.
-- It is never stored in Silver and never averaged across days. The live data
-- shows why: over 31 days one media ran 6455 loads / 48 plays (0.74%) and the
-- other 44 loads / 18 plays (40.91%). Averaging those two rates gives ~21%; the
-- correct volume-weighted figure is 66/6499 = 1.02%. A 20x error that looks
-- entirely plausible on a chart.
--
-- So every rolling and to-date figure below divides SUM(plays) by SUM(loads),
-- never AVG(daily_rate).
--
-- FEASIBILITY NOTE — play_rate PER CHANNEL IS NOT COMPUTABLE.
-- The stats endpoint is grained on hashed_id + date with no visitor identity
-- and no channel attribute, so there is no key to join aggregate video stats to
-- a marketing channel. The dimension simply is not in the payload. The honest
-- proxy is the matched subset in video_booking_funnel — visitors whose email
-- joins a channel-tagged booking — reported with its match rate, which is
-- currently zero. Treating each media_id as a channel would be fabrication:
-- there are two media and three channels, and they do not correspond.
-- =============================================================================

CREATE TABLE insightflow_gold.media_engagement__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{gold_bucket}}/media_engagement/build_id={{build_id}}/'
) AS

WITH daily AS (
  SELECT
    s.hashed_id,
    s.stat_date,
    m.media_name,
    m.duration_minutes,
    s.load_count,
    s.play_count,
    s.hours_watched,
    s.is_zero_activity_day
  FROM insightflow_silver.fct_media_daily_stats s
  LEFT JOIN insightflow_silver.dim_media m
    ON m.hashed_id = s.hashed_id
)

SELECT
  hashed_id,
  media_name,
  stat_date,
  duration_minutes,

  -- Additive components, always carried so any consumer can re-aggregate
  -- correctly at their own grain.
  load_count,
  play_count,
  hours_watched,
  is_zero_activity_day,

  -- Divided once, at this row's grain.
  CASE WHEN load_count > 0
       THEN ROUND(CAST(play_count AS DOUBLE) / load_count, 4)
  END                                         AS play_rate,

  -- Rolling figures divide summed components, NOT an average of daily rates.
  ROUND(
    CAST(SUM(play_count) OVER (
      PARTITION BY hashed_id ORDER BY stat_date
      ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS DOUBLE)
    / NULLIF(SUM(load_count) OVER (
      PARTITION BY hashed_id ORDER BY stat_date
      ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 0)
  , 4)                                        AS play_rate_7d,

  ROUND(
    CAST(SUM(play_count) OVER (PARTITION BY hashed_id ORDER BY stat_date) AS DOUBLE)
    / NULLIF(SUM(load_count) OVER (PARTITION BY hashed_id ORDER BY stat_date), 0)
  , 4)                                        AS play_rate_to_date,

  SUM(play_count) OVER (PARTITION BY hashed_id ORDER BY stat_date) AS plays_cumulative,
  SUM(load_count) OVER (PARTITION BY hashed_id ORDER BY stat_date) AS loads_cumulative,

  -- Average watch time per play, again components divided once rather than an
  -- average of per-day averages.
  CASE WHEN play_count > 0
       THEN ROUND(hours_watched * 60.0 / play_count, 2)
  END                                         AS avg_minutes_per_play,

  DATE '{{asof}}'                             AS build_date

FROM daily;
