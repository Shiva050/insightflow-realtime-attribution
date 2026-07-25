-- =============================================================================
-- Bronze external tables
-- =============================================================================
-- Registers the raw S3 objects in the Glue Data Catalog so Athena can read
-- them. Creates no data and copies nothing: Bronze remains the immutable
-- objects the ingest Lambdas wrote.
--
-- WHY ONE `raw string` COLUMN INSTEAD OF A TYPED SCHEMA
--
-- Every table below reads each NDJSON line as a single opaque string, and
-- Silver parses it with json_extract_scalar. That is deliberate:
--
--   * The CRM payload carries keys like `custom.cf_am3Ug...`. Dots in key
--     names fight Hive column naming, and the field IDs are opaque vendor
--     strings that belong in a seed, not in DDL.
--   * Sources add fields without warning. A typed SerDe schema silently
--     returns NULL for anything it does not know about, and breaks outright on
--     a type change. A string column cannot drift.
--   * Calendly nests several levels deep and includes arrays that are their own
--     grain (event_memberships). Flattening those in DDL would bake a grain
--     decision into the raw layer.
--
-- This is schema-on-read in the strict sense: Bronze stores bytes, Silver
-- decides what they mean. It is also what makes a Bronze replay meaningful —
-- re-running Silver against unchanged Bronze can fix a parsing mistake without
-- re-ingesting anything.
--
-- PARTITION PROJECTION, NOT A CRAWLER
--
-- Partitions are computed from the key layout rather than discovered. No Glue
-- crawler to schedule, no MSCK REPAIR TABLE to forget after each load, and no
-- window where fresh data is invisible because the catalog has not caught up.
-- The trade is that the layout must match the template exactly.
--
-- Run once, in order, after 00_create_database.sql.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- CRM events  (grain: event_id)
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS insightflow_bronze.crm_event (
  raw string
)
PARTITIONED BY (dt string)
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY '\001'      -- a byte JSON never contains, so the whole
                                   -- line lands in one column
LOCATION 's3://insightflow-bronze/crm/events/'
TBLPROPERTIES (
  'projection.enabled'                = 'true',
  'projection.dt.type'                = 'date',
  'projection.dt.format'              = 'yyyy-MM-dd',
  'projection.dt.range'               = '2025-01-01,NOW',
  'projection.dt.interval'            = '1',
  'projection.dt.interval.unit'       = 'DAYS',
  'storage.location.template'         = 's3://insightflow-bronze/crm/events/dt=${dt}'
);


-- -----------------------------------------------------------------------------
-- Calendly bookings  (grain: invitee_uri)
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS insightflow_bronze.calendly_booking (
  raw string
)
PARTITIONED BY (dt string)
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY '\001'
LOCATION 's3://insightflow-bronze/calendly/bookings/'
TBLPROPERTIES (
  'projection.enabled'                = 'true',
  'projection.dt.type'                = 'date',
  'projection.dt.format'              = 'yyyy-MM-dd',
  'projection.dt.range'               = '2025-01-01,NOW',
  'projection.dt.interval'            = '1',
  'projection.dt.interval.unit'       = 'DAYS',
  'storage.location.template'         = 's3://insightflow-bronze/calendly/bookings/dt=${dt}'
);


-- -----------------------------------------------------------------------------
-- Calendly cancellations  (grain: invitee_uri, partitioned on cancellation date)
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS insightflow_bronze.calendly_cancellation (
  raw string
)
PARTITIONED BY (dt string)
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY '\001'
LOCATION 's3://insightflow-bronze/calendly/cancellations/'
TBLPROPERTIES (
  'projection.enabled'                = 'true',
  'projection.dt.type'                = 'date',
  'projection.dt.format'              = 'yyyy-MM-dd',
  'projection.dt.range'               = '2025-01-01,NOW',
  'projection.dt.interval'            = '1',
  'projection.dt.interval.unit'       = 'DAYS',
  'storage.location.template'         = 's3://insightflow-bronze/calendly/cancellations/dt=${dt}'
);


-- -----------------------------------------------------------------------------
-- Calendly spend  (grain: one row per date+channel, MANY files per date)
--
-- Partitioned on `asof`, the file's publication date — NOT the spend date.
-- Each file carries a 30-day trailing window, so one spend date appears in up
-- to 30 partitions. Silver resolves the overlap last-write-wins ordered by
-- asof. Anyone who sums this table without that collapse inflates spend up to
-- 30x. See SOURCE_CONTRACTS.md.
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS insightflow_bronze.calendly_spend (
  raw string
)
PARTITIONED BY (asof string)
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY '\001'
LOCATION 's3://insightflow-bronze/calendly/spend/'
TBLPROPERTIES (
  'projection.enabled'                = 'true',
  'projection.asof.type'              = 'date',
  'projection.asof.format'            = 'yyyy-MM-dd',
  'projection.asof.range'             = '2025-01-01,NOW',
  'projection.asof.interval'          = '1',
  'projection.asof.interval.unit'     = 'DAYS',
  'storage.location.template'         = 's3://insightflow-bronze/calendly/spend/asof=${asof}'
);


-- -----------------------------------------------------------------------------
-- Wistia media metadata  (grain: hashed_id, daily snapshot)
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS insightflow_bronze.wistia_media (
  raw string
)
PARTITIONED BY (asof string)
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY '\001'
LOCATION 's3://insightflow-bronze/wistia/media/'
TBLPROPERTIES (
  'projection.enabled'                = 'true',
  'projection.asof.type'              = 'date',
  'projection.asof.format'            = 'yyyy-MM-dd',
  'projection.asof.range'             = '2025-01-01,NOW',
  'projection.asof.interval'          = '1',
  'projection.asof.interval.unit'     = 'DAYS',
  'storage.location.template'         = 's3://insightflow-bronze/wistia/media/asof=${asof}'
);


-- -----------------------------------------------------------------------------
-- Wistia daily media stats  (grain: hashed_id + date — a FLOW, not a stock)
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS insightflow_bronze.wistia_media_stats (
  raw string
)
PARTITIONED BY (dt string)
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY '\001'
LOCATION 's3://insightflow-bronze/wistia/media_stats/'
TBLPROPERTIES (
  'projection.enabled'                = 'true',
  'projection.dt.type'                = 'date',
  'projection.dt.format'              = 'yyyy-MM-dd',
  'projection.dt.range'               = '2025-01-01,NOW',
  'projection.dt.interval'            = '1',
  'projection.dt.interval.unit'       = 'DAYS',
  'storage.location.template'         = 's3://insightflow-bronze/wistia/media_stats/dt=${dt}'
);


-- -----------------------------------------------------------------------------
-- Wistia visitor events  (grain: event_key — the video->lead bridge)
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS insightflow_bronze.wistia_visitor_event (
  raw string
)
PARTITIONED BY (dt string)
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY '\001'
LOCATION 's3://insightflow-bronze/wistia/visitor_events/'
TBLPROPERTIES (
  'projection.enabled'                = 'true',
  'projection.dt.type'                = 'date',
  'projection.dt.format'              = 'yyyy-MM-dd',
  'projection.dt.range'               = '2025-01-01,NOW',
  'projection.dt.interval'            = '1',
  'projection.dt.interval.unit'       = 'DAYS',
  'storage.location.template'         = 's3://insightflow-bronze/wistia/visitor_events/dt=${dt}'
);


-- -----------------------------------------------------------------------------
-- Seeds  (externalised reference maps — config, not code)
--
-- Unpartitioned: two tiny files that change when a campaign is onboarded, not
-- on a schedule.
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS insightflow_bronze.seed_channel_map (
  raw string
)
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY '\001'
LOCATION 's3://insightflow-bronze/seeds/channel_map/';

CREATE EXTERNAL TABLE IF NOT EXISTS insightflow_bronze.seed_custom_field_map (
  raw string
)
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY '\001'
LOCATION 's3://insightflow-bronze/seeds/custom_field_map/';
