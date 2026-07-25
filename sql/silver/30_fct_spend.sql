-- =============================================================================
-- fct_spend  ·  grain: spend_date + channel  ·  periodic snapshot fact
-- =============================================================================
-- ⚠️ THE DEDUPLICATION HERE IS LOAD-BEARING.
--
-- The source publishes a file per day, and EACH FILE CARRIES A 30-DAY TRAILING
-- WINDOW. The filename date is a publication date, not the data's date, so one
-- (spend_date, channel) appears in up to 30 Bronze partitions.
--
-- Summing Bronze without this collapse inflates spend up to 30x. Nothing
-- errors; CPB just silently reports every channel as catastrophically
-- expensive. This was found by checking the live source rather than reading the
-- spec, which describes "daily for Day-1 spends" and implies one day per file.
-- See SOURCE_CONTRACTS.md.
--
-- Resolution is last-write-wins ordered by AS-OF DATE: the newest file wins,
-- because corrections propagate forward into every later window. Same principle
-- as ordering the lead merge on event time — "last" has to mean something
-- defensible, and here that is publication order.
--
-- NOT JOINED TO BOOKINGS HERE. That join is metric-specific (CPB) and belongs
-- in Gold. Silver supplies what makes it possible: a conformed channel label
-- and a clean date grain.
-- =============================================================================

CREATE TABLE insightflow_silver.fct_spend__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{silver_bucket}}/fct_spend/build_id={{build_id}}/'
) AS

WITH parsed AS (
  SELECT
    json_extract_scalar(raw, '$.date')              AS spend_date_str,
    json_extract_scalar(raw, '$.channel')           AS channel,
    TRY(CAST(json_extract_scalar(raw, '$.spend') AS DOUBLE)) AS spend,
    asof                                            AS asof_date
  FROM insightflow_bronze.calendly_spend
  WHERE json_extract_scalar(raw, '$.date') IS NOT NULL
    AND json_extract_scalar(raw, '$.channel') IS NOT NULL
),

deduped AS (
  SELECT *
  FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (
        PARTITION BY spend_date_str, channel
        ORDER BY asof_date DESC          -- newest publication wins
      ) AS rn,
      COUNT(*) OVER (
        PARTITION BY spend_date_str, channel
      ) AS source_file_count
    FROM parsed
  )
  WHERE rn = 1
),

channel_map AS (
  SELECT
    json_extract_scalar(raw, '$.channel')                   AS channel,
    CAST(json_extract_scalar(raw, '$.is_paid') AS BOOLEAN)  AS is_paid
  FROM insightflow_bronze.seed_channel_map
)

SELECT
  CAST(d.spend_date_str AS DATE)                    AS spend_date,
  d.channel,
  d.spend,
  COALESCE(cm.is_paid, TRUE)                        AS is_paid_channel,

  -- Provenance for the value we kept: which file it came from, and how many
  -- files offered a value for this key. If source_file_count is 1 for an old
  -- date, the window no longer covers it and later corrections will not arrive.
  d.asof_date                                       AS sourced_from_asof,
  d.source_file_count,

  DATE '{{asof}}'                                   AS build_date

FROM deduped d
LEFT JOIN channel_map cm ON cm.channel = d.channel;
