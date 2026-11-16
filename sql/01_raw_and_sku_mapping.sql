-- =============================================================================
-- 01  RAW LANDING, SKU STANDARDIZATION AND ERROR HANDLING   (Snowflake, illustrative)
-- Not executed by the local prototype (see src/zaar_assistant/ingest.py for a runnable mirror).
--
-- Design rule: RAW accepts everything; nothing is rejected at the door and nothing is dropped.
-- Every landed row is classified exactly once: VALID, or rejected with an explicit reason.
--   landing rows = valid rows + quarantined rows        (checked in 02, fan-out detected)
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS curated;
CREATE SCHEMA IF NOT EXISTS ops;

-- Immutable raw landing: append-only, loosely typed, one row per source record.
-- Fivetran (Shopify), scheduled Google Sheets loads (hospital B2B) and Snowpipe (S3 retailer
-- files) all land in the same shape. Keys and quantities are nullable ON PURPOSE: a malformed
-- record must land so that it can be seen, counted and fixed, not vanish in a failed load.
CREATE TABLE IF NOT EXISTS raw.orders_landing (
    landing_id      NUMBER AUTOINCREMENT,
    source_system   STRING        NOT NULL,   -- 'shopify' | 'gsheets_b2b' | 's3_retail'
    source_order_id STRING,
    source_line_id  STRING,
    source_sku      STRING,
    quantity        NUMBER(18, 0),
    event_type      STRING,                   -- 'order' | 'cancellation' | 'return' | 'correction'
    event_ts        TIMESTAMP_NTZ,
    ingested_at     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    batch_id        STRING        NOT NULL,
    raw_payload     VARIANT
);

-- Canonical SKU map: every (source system, source SKU) -> one canonical SKU.
-- NOTE: Snowflake does not enforce PRIMARY KEY. A duplicated map key would silently multiply
-- landing rows in the LEFT JOIN below, so uniqueness is verified by a check in 02.
CREATE TABLE IF NOT EXISTS curated.sku_map (
    source_system STRING  NOT NULL,
    source_sku    STRING  NOT NULL,
    canonical_sku STRING  NOT NULL,
    is_active     BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (source_system, source_sku)
);

-- Error table: rejected rows are parked with a reason and a lifecycle, never dropped.
CREATE TABLE IF NOT EXISTS curated.orders_quarantine (
    landing_id      NUMBER        NOT NULL,   -- points back at the immutable raw row
    source_system   STRING,
    source_order_id STRING,
    source_line_id  STRING,
    source_sku      STRING,
    quantity        NUMBER(18, 0),
    event_type      STRING,
    event_ts        TIMESTAMP_NTZ,
    batch_id        STRING,
    reject_reason   STRING        NOT NULL,   -- see CASE below
    status          STRING        DEFAULT 'OPEN',   -- OPEN | RESOLVED
    quarantined_at  TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    resolved_at     TIMESTAMP_NTZ
);

-- -----------------------------------------------------------------------------
-- Classification: LEFT JOIN so that unmapped rows SURVIVE the join (an INNER JOIN would
-- silently drop exactly the rows we most need to see). The first rule that fires wins.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW curated.orders_classified AS
SELECT
    l.landing_id, l.source_system, l.source_order_id, l.source_line_id, l.source_sku,
    l.quantity, l.event_type, l.event_ts, l.batch_id, l.ingested_at,
    m.canonical_sku,
    CASE
        WHEN NULLIF(TRIM(l.source_order_id), '') IS NULL
          OR NULLIF(TRIM(l.source_line_id),  '') IS NULL            THEN 'MISSING_KEY'
        WHEN NULLIF(TRIM(l.source_sku), '') IS NULL                 THEN 'MISSING_SKU'
        WHEN m.canonical_sku IS NULL                                THEN 'UNMAPPED_SKU'      -- fixable: add to map, replay
        WHEN l.quantity IS NULL                                     THEN 'NULL_QUANTITY'
        WHEN l.quantity < 0                                         THEN 'NEGATIVE_QUANTITY'
        WHEN l.event_type IS NULL
          OR l.event_type NOT IN ('order', 'cancellation', 'return', 'correction')
                                                                    THEN 'UNKNOWN_EVENT_TYPE'
        WHEN l.event_ts IS NULL                                     THEN 'MISSING_TIMESTAMP'
    END AS reject_reason                                            -- NULL = valid
FROM raw.orders_landing l
LEFT JOIN curated.sku_map m
       ON m.source_system = l.source_system
      AND m.source_sku    = l.source_sku
      AND m.is_active;

-- Standardized view: valid rows only, deduplicated on the natural key of an event
-- (latest ingestion wins).
CREATE OR REPLACE VIEW curated.orders_standardized AS
SELECT
    source_system, source_order_id, source_line_id,
    canonical_sku AS sku,
    quantity, event_type, event_ts, batch_id
FROM curated.orders_classified
WHERE reject_reason IS NULL
QUALIFY ROW_NUMBER() OVER (
          PARTITION BY source_system, source_order_id, source_line_id, event_type, batch_id
          ORDER BY ingested_at DESC, landing_id DESC) = 1;

-- Operations view: what is currently broken, by reason and age. Alerts in 02 read this.
CREATE OR REPLACE VIEW ops.quarantine_summary AS
SELECT
    reject_reason,
    COUNT(*)                                            AS open_rows,
    MIN(quarantined_at)                                 AS oldest_open,
    DATEDIFF('hour', MIN(quarantined_at), CURRENT_TIMESTAMP()) AS oldest_open_hours
FROM curated.orders_quarantine
WHERE status = 'OPEN'
GROUP BY reject_reason;
