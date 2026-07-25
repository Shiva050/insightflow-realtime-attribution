-- =============================================================================
-- dq_lead_coverage  ·  grain: build date
-- =============================================================================
-- Coverage as a monitored metric, not a one-time check.
--
-- If 40% of leads have no email, the funnel silently describes only the other
-- 60% — no error, no hint, a confident wrong number. This table exists so that
-- failure is visible.
--
-- Its real value is the TRAJECTORY. A match rate sliding from 85% to 60%
-- overnight means the owner sweep broke, and you learn it here rather than from
-- a stakeholder. That is why the owner export is snapshotted per asof= instead
-- of overwritten, and why this table appends a row per build rather than being
-- replaced.
--
-- One row per build. Rebuilt each run from all prior builds plus today, so the
-- series survives a full-rebuild Silver.
-- =============================================================================

CREATE TABLE insightflow_silver.dq_lead_coverage__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{silver_bucket}}/dq_lead_coverage/build_id={{build_id}}/'
) AS

WITH history AS (
  -- Every prior build's snapshot, so the trend line is not lost when Silver is
  -- recomputed. Each asof= partition is one day's exported owner state.
  SELECT
    o.asof                                                AS build_date_str,
    COUNT(*)                                              AS owners_known
  FROM insightflow_bronze.lead_owner o
  GROUP BY o.asof
),

awaiting AS (
  SELECT
    a.asof                                                AS build_date_str,
    COUNT(*)                                              AS awaiting_total,
    COUNT_IF(json_extract_scalar(a.raw, '$.status') = 'AWAITING')   AS awaiting_open,
    COUNT_IF(json_extract_scalar(a.raw, '$.status') = 'EXHAUSTED')  AS awaiting_exhausted,
    -- How long the oldest unresolved lead has been waiting. A rising figure
    -- means the sweep is running but the source never publishes those owners.
    MAX(CAST(json_extract_scalar(a.raw, '$.retry_count') AS INTEGER)) AS max_retry_count
  FROM insightflow_bronze.awaiting_owner a
  GROUP BY a.asof
),

leads_today AS (
  SELECT
    COUNT(*)                        AS total_leads,
    COUNT_IF(has_email)             AS leads_with_email,
    COUNT_IF(has_owner)             AS leads_with_owner
  FROM insightflow_silver.dim_lead__{{build_id}}
)

SELECT
  DATE '{{asof}}'                                          AS build_date,

  l.total_leads,
  l.leads_with_email,
  l.leads_with_owner,

  -- Guarded divide. A rate of NULL means "no leads yet"; a rate of 0 means
  -- "leads exist and none matched". Rendering the first as 0 would report a
  -- catastrophic data-quality failure that had not happened.
  CASE WHEN l.total_leads > 0
       THEN ROUND(CAST(l.leads_with_email AS DOUBLE) / l.total_leads, 4)
  END                                                      AS email_match_rate,
  CASE WHEN l.total_leads > 0
       THEN ROUND(CAST(l.leads_with_owner AS DOUBLE) / l.total_leads, 4)
  END                                                      AS owner_match_rate,

  COALESCE(a.awaiting_open, 0)                             AS awaiting_owner_open,
  COALESCE(a.awaiting_exhausted, 0)                        AS awaiting_owner_exhausted,
  a.max_retry_count,

  COALESCE(h.owners_known, 0)                              AS owner_cache_size,

  -- The bias direction, carried with the number. Unmatched rows are not
  -- random: they skew toward recent leads whose owner assignment has not
  -- landed, so the matched subset over-represents older, fully-processed leads.
  'unmatched leads skew recent - owner assignment has not landed yet'
                                                           AS coverage_caveat

FROM leads_today l
LEFT JOIN awaiting a ON a.build_date_str = '{{asof}}'
LEFT JOIN history  h ON h.build_date_str = '{{asof}}';
