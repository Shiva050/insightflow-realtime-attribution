-- =============================================================================
-- dim_lead  ·  grain: lead_id  ·  SCD Type 1
-- =============================================================================
-- Type 1 because nothing needs lead status history yet — and because Bronze
-- keeps the full immutable event flow, this can be rebuilt as Type 2 later
-- without re-ingesting anything. The history is deferred, not lost.
--
-- THE MERGE ORDERS ON EVENT TIME, NOT ARRIVAL TIME
-- Type 1 is last-write-wins, so "last" has to be defined correctly. Arrival
-- order is untrustworthy here: webhook retries, SQS redelivery, parallel
-- Lambdas and DLQ replays all land events out of order, and the Bronze dt=
-- partition is derived from the payload precisely so it cannot be used as a
-- proxy for processing order either. We order on date_updated from the event
-- body and tiebreak on event_id, so the merge is reproducible across runs.
--
-- FIELD-LEVEL SOURCE OF TRUTH
-- Overlapping fields come from the EVENT, which has full coverage — every lead
-- has an event, that is how it entered the system. The owner export supplies
-- only what exists nowhere else: lead_owner and lead_email. Sourcing funnel
-- from the lookup would null it out for every owner-less lead, when the value
-- sits in the event payload all along.
-- =============================================================================

CREATE TABLE insightflow_silver.dim_lead__{{build_id}}
WITH (
  format               = 'PARQUET',
  parquet_compression  = 'SNAPPY',
  external_location    = 's3://{{silver_bucket}}/dim_lead/build_id={{build_id}}/'
) AS

WITH events AS (
  SELECT
    json_extract_scalar(raw, '$.event.id')                            AS event_id,
    json_extract_scalar(raw, '$.event.lead_id')                       AS lead_id,
    json_extract_scalar(raw, '$.event.action')                        AS action,
    json_extract_scalar(raw, '$.event.date_updated')                  AS event_updated_at_raw,
    json_extract_scalar(raw, '$.event.data.display_name')             AS display_name,
    json_extract_scalar(raw, '$.event.data.status_label')             AS status_label,
    json_extract_scalar(raw, '$.event.data.date_created')             AS lead_created_at_raw,
    json_extract_scalar(raw, '$.event.data.date_updated')             AS lead_updated_at_raw,
    -- The funnel field ID is an opaque vendor string held in a seed and
    -- templated in at build time. Bracket notation because the key itself
    -- contains a dot: "custom.cf_..." is ONE key, not a nested path.
    json_extract_scalar(raw, '$.event.data["{{funnel_field_id}}"]')   AS funnel
  FROM insightflow_bronze.crm_event
  WHERE json_extract_scalar(raw, '$.event.lead_id') IS NOT NULL
),

ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY lead_id
      ORDER BY
        -- TRY so one malformed timestamp cannot fail the whole build; such a
        -- row sorts last rather than winning the merge by accident.
        TRY(from_iso8601_timestamp(event_updated_at_raw)) DESC NULLS LAST,
        event_id DESC
    ) AS rn
  FROM events
),

owner AS (
  SELECT
    json_extract_scalar(raw, '$.lead_id')    AS lead_id,
    json_extract_scalar(raw, '$.lead_owner') AS lead_owner,
    json_extract_scalar(raw, '$.lead_email') AS lead_email
  FROM insightflow_bronze.lead_owner
  WHERE asof = '{{asof}}'
)

SELECT
  e.lead_id,
  e.display_name,
  e.status_label,
  e.funnel,
  e.action                                          AS last_action,

  o.lead_owner,
  o.lead_email,
  -- Normalised ONCE here, alongside the raw form, and on both sides of the
  -- bridge join. Leaving it to each Gold query means one forgotten lower()
  -- silently under-joins, and correctness should not depend on every analyst
  -- remembering. The raw value stays so a rule change can be recomputed.
  LOWER(TRIM(o.lead_email))                         AS lead_email_normalized,

  TRY(from_iso8601_timestamp(e.lead_created_at_raw))  AS lead_created_at,
  TRY(from_iso8601_timestamp(e.lead_updated_at_raw))  AS lead_updated_at,

  -- Coverage is a column on the dimension, not a report someone remembers to
  -- run. A silent join drop is a correctness bug that looks like a number.
  (o.lead_email IS NOT NULL)                        AS has_email,
  (o.lead_owner IS NOT NULL)                        AS has_owner,
  CASE
    WHEN o.lead_email IS NOT NULL THEN 'ENRICHED'
    WHEN o.lead_owner IS NOT NULL THEN 'OWNER_ONLY'
    ELSE 'AWAITING_OWNER'
  END                                               AS enrichment_status,

  e.event_id                                        AS source_event_id,
  -- Stamped because Silver is recomputed: the same historical day can show a
  -- different figure across builds as late enrichment lands. That is correct
  -- behaviour, but anyone comparing two screenshots needs to see which build
  -- each came from.
  DATE '{{asof}}'                                   AS build_date

FROM ranked e
LEFT JOIN owner o
  ON o.lead_id = e.lead_id
WHERE e.rn = 1;
