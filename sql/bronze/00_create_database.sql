-- =============================================================================
-- Glue databases
-- =============================================================================
-- Three databases rather than one, so the medallion boundary is enforced by
-- naming rather than by convention. A Gold query that reaches into
-- insightflow_bronze is visibly wrong in the SQL itself.
--
-- Run once, before anything else.
-- =============================================================================

CREATE DATABASE IF NOT EXISTS insightflow_bronze
COMMENT 'Raw immutable source records, NDJSON, schema-on-read';

CREATE DATABASE IF NOT EXISTS insightflow_silver
COMMENT 'Conformed, deduplicated, enriched. Finest useful grain, kept wide.';

CREATE DATABASE IF NOT EXISTS insightflow_gold
COMMENT 'Per-metric marts, shaped for the dashboard.';
