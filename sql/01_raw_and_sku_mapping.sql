-- =============================================================================
-- 01  RAW LANDING + SKU STANDARDIZATION   (Snowflake dialect, illustrative)
-- Not executed by the local prototype; shows the shape of the production design.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS curated;

-- Immutable raw landing: append-only, one row per source record.
-- Fivetran (Shopify), Google Sheets ingestion (hospital B2B) and Snowpipe (S3 retailer files)
-- all land in the same shape so downstream logic is source-agnostic.
CREATE TABLE IF NOT EXISTS raw.orders_landing (
    source_system   STRING        NOT NULL,   -- 'shopify' | 'gsheets_b2b' | 's3_retail'
    source_order_id STRING        NOT NULL,
    source_line_id  STRING        NOT NULL,
    source_sku      STRING,
    quantity        NUMBER(18, 0),
    event_type      STRING,                   -- 'order' | 'cancellation' | 'return' | 'correction'
    event_ts        TIMESTAMP_NTZ,
    ingested_at     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    batch_id        STRING        NOT NULL,
    raw_payload     VARIANT
);

-- Canonical SKU map: every source identifier -> one canonical SKU.
CREATE TABLE IF NOT EXISTS curated.sku_map (
    source_system STRING NOT NULL,
    source_sku    STRING NOT NULL,
    canonical_sku STRING NOT NULL,
    PRIMARY KEY (source_system, source_sku)
);

-- Quarantine: anything unmapped or malformed is parked, counted and alerted on,
-- never silently dropped and never allowed into planning.
CREATE TABLE IF NOT EXISTS curated.orders_quarantine (
    LIKE raw.orders_landing,
    reject_reason STRING,
    quarantined_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Standardized view: mapped SKUs, deduplicated by (source_system, order_id, line_id,
-- event_type), keeping the latest ingestion of each natural key.
CREATE OR REPLACE VIEW curated.orders_standardized AS
SELECT
    l.source_system,
    l.source_order_id,
    l.source_line_id,
    m.canonical_sku                                  AS sku,
    l.quantity,
    l.event_type,
    l.event_ts,
    l.batch_id
FROM raw.orders_landing l
JOIN curated.sku_map    m
  ON m.source_system = l.source_system
 AND m.source_sku    = l.source_sku
WHERE l.quantity IS NOT NULL
  AND l.quantity >= 0
QUALIFY ROW_NUMBER() OVER (
          PARTITION BY l.source_system, l.source_order_id, l.source_line_id, l.event_type
          ORDER BY l.ingested_at DESC) = 1;

-- Rows that failed the join / validity filters are routed to quarantine.
INSERT INTO curated.orders_quarantine
SELECT l.*, 'UNMAPPED_SKU_OR_INVALID_QTY', CURRENT_TIMESTAMP()
FROM raw.orders_landing l
LEFT JOIN curated.sku_map m
  ON m.source_system = l.source_system AND m.source_sku = l.source_sku
WHERE m.canonical_sku IS NULL OR l.quantity IS NULL OR l.quantity < 0;
