-- NOTE: Snowflake-dialect reference script; read it alongside docs/architecture.md.
-- =============================================================================
-- 02  CHANGE PROCESSING, QUARANTINE, REPLAY AND CHECKS   (Snowflake, illustrative)
-- Cancellations, returns and corrected orders flow through as CHANGES, not rebuilds.
-- Rejected rows go to quarantine in the SAME transaction that applies the valid rows.
-- =============================================================================

CREATE TABLE IF NOT EXISTS curated.order_lines (
    source_system   STRING,
    source_order_id STRING,
    source_line_id  STRING,
    sku             STRING,
    net_quantity    NUMBER(18, 0),   -- after cancellations / returns / corrections
    last_event_ts   TIMESTAMP_NTZ,
    last_batch_id   STRING,
    PRIMARY KEY (source_system, source_order_id, source_line_id)
);

CREATE TABLE IF NOT EXISTS ops.pipeline_checks (
    check_name  STRING,
    expected    NUMBER,
    actual      NUMBER,
    status      STRING,               -- OK | WARN | FAIL
    checked_at  TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- The stream exposes only rows that landed since the last successful consume.
CREATE OR REPLACE STREAM raw.orders_landing_stream ON TABLE raw.orders_landing APPEND_ONLY = TRUE;

-- Same classification as curated.orders_classified, applied to the stream. Kept in sync by hand
-- here for readability; in production both are generated from one dbt macro.
CREATE OR REPLACE VIEW curated.new_orders_classified AS
SELECT
    s.landing_id, s.source_system, s.source_order_id, s.source_line_id, s.source_sku,
    s.quantity, s.event_type, s.event_ts, s.batch_id, s.ingested_at,
    m.canonical_sku,
    CASE
        WHEN NULLIF(TRIM(s.source_order_id), '') IS NULL
          OR NULLIF(TRIM(s.source_line_id),  '') IS NULL            THEN 'MISSING_KEY'
        WHEN NULLIF(TRIM(s.source_sku), '') IS NULL                 THEN 'MISSING_SKU'
        WHEN m.canonical_sku IS NULL                                THEN 'UNMAPPED_SKU'
        WHEN s.quantity IS NULL                                     THEN 'NULL_QUANTITY'
        WHEN s.quantity < 0                                         THEN 'NEGATIVE_QUANTITY'
        WHEN s.event_type IS NULL
          OR s.event_type NOT IN ('order', 'cancellation', 'return', 'correction')
                                                                    THEN 'UNKNOWN_EVENT_TYPE'
        WHEN s.event_ts IS NULL                                     THEN 'MISSING_TIMESTAMP'
    END AS reject_reason
FROM raw.orders_landing_stream s
LEFT JOIN curated.sku_map m
       ON m.source_system = s.source_system AND m.source_sku = s.source_sku AND m.is_active;

-- One procedure, one transaction. Two tasks reading the same stream would be wrong: the first
-- DML advances the stream offset and the second would see nothing. Inside a single transaction
-- both statements see the same stream contents, and the offset advances only on COMMIT, so a
-- failure rolls back BOTH the quarantine inserts and the MERGE and the rows are re-read next run.
CREATE OR REPLACE PROCEDURE curated.process_order_changes()
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
    BEGIN TRANSACTION;

    -- 1) rejects -> quarantine, with the reason
    INSERT INTO curated.orders_quarantine
        (landing_id, source_system, source_order_id, source_line_id, source_sku,
         quantity, event_type, event_ts, batch_id, reject_reason)
    SELECT landing_id, source_system, source_order_id, source_line_id, source_sku,
           quantity, event_type, event_ts, batch_id, reject_reason
    FROM curated.new_orders_classified
    WHERE reject_reason IS NOT NULL;

    -- 2) valid rows -> order_lines (deduped, then one row per line so MERGE is deterministic)
    MERGE INTO curated.order_lines t
    USING (
        SELECT
            source_system, source_order_id, source_line_id,
            ANY_VALUE(canonical_sku) AS sku,
            SUM(CASE event_type WHEN 'order' THEN quantity
                                WHEN 'cancellation' THEN -quantity
                                WHEN 'return'       THEN -quantity ELSE 0 END)     AS delta_qty,
            MAX_BY(IFF(event_type = 'correction', quantity, NULL),
                   IFF(event_type = 'correction', event_ts, NULL))                  AS corrected_qty,
            MAX(event_ts) AS event_ts,
            MAX(batch_id) AS batch_id
        FROM (
            SELECT * FROM curated.new_orders_classified
            WHERE reject_reason IS NULL
            QUALIFY ROW_NUMBER() OVER (
                      PARTITION BY source_system, source_order_id, source_line_id, event_type, batch_id
                      ORDER BY ingested_at DESC, landing_id DESC) = 1
        )
        GROUP BY source_system, source_order_id, source_line_id
    ) c
    ON  t.source_system   = c.source_system
    AND t.source_order_id = c.source_order_id
    AND t.source_line_id  = c.source_line_id
    WHEN MATCHED AND c.batch_id <> t.last_batch_id THEN UPDATE SET
        t.net_quantity  = COALESCE(c.corrected_qty, t.net_quantity + c.delta_qty),
        t.last_event_ts = c.event_ts,
        t.last_batch_id = c.batch_id
    WHEN NOT MATCHED THEN INSERT
        (source_system, source_order_id, source_line_id, sku, net_quantity, last_event_ts, last_batch_id)
    VALUES
        (c.source_system, c.source_order_id, c.source_line_id, c.sku,
         COALESCE(c.corrected_qty, c.delta_qty), c.event_ts, c.batch_id);

    COMMIT;
    RETURN 'ok';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

CREATE OR REPLACE TASK curated.merge_order_changes
    WAREHOUSE = PLANNING_WH
    SCHEDULE  = '15 MINUTE'
    WHEN SYSTEM$STREAM_HAS_DATA('raw.orders_landing_stream')
AS
    CALL curated.process_order_changes();

-- -----------------------------------------------------------------------------
-- Replay: once the SKU map is fixed, UNMAPPED_SKU rows are re-landed as a NEW batch and flow
-- through the normal path. RAW stays immutable (the original row is never edited).
-- Malformed rows (MISSING_KEY, NULL_QUANTITY, ...) cannot be fixed by a map change: they stay
-- OPEN until the source is corrected and re-ingested, which creates a new valid landing row.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE curated.replay_unmapped_skus()
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
    BEGIN TRANSACTION;

    INSERT INTO raw.orders_landing
        (source_system, source_order_id, source_line_id, source_sku,
         quantity, event_type, event_ts, batch_id)
    SELECT q.source_system, q.source_order_id, q.source_line_id, q.source_sku,
           q.quantity, q.event_type, q.event_ts,
           'REPLAY-' || TO_VARCHAR(CURRENT_TIMESTAMP(), 'YYYYMMDDHH24MISS')
    FROM curated.orders_quarantine q
    JOIN curated.sku_map m
      ON m.source_system = q.source_system AND m.source_sku = q.source_sku AND m.is_active
    WHERE q.status = 'OPEN' AND q.reject_reason = 'UNMAPPED_SKU';

    UPDATE curated.orders_quarantine q
    SET status = 'RESOLVED', resolved_at = CURRENT_TIMESTAMP()
    FROM curated.sku_map m
    WHERE q.status = 'OPEN' AND q.reject_reason = 'UNMAPPED_SKU'
      AND m.source_system = q.source_system AND m.source_sku = q.source_sku AND m.is_active;

    COMMIT;
    RETURN 'ok';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

-- Run after an analyst adds the missing mapping rows (or schedule it hourly).
CREATE OR REPLACE TASK curated.replay_quarantine
    WAREHOUSE = PLANNING_WH
    SCHEDULE  = '60 MINUTE'
AS
    CALL curated.replay_unmapped_skus();

-- -----------------------------------------------------------------------------
-- Checks and alerts (thresholds illustrative). Any WARN/FAIL is picked up by the notification
-- integration and shown in the operations view; a FAIL also blocks the assistant via freshness.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TASK curated.run_pipeline_checks
    WAREHOUSE = PLANNING_WH
    AFTER curated.merge_order_changes
AS
INSERT INTO ops.pipeline_checks (check_name, expected, actual, status)
-- 1) classification must be 1:1 with landing. More rows than landing means the LEFT JOIN fanned
--    out because sku_map has a duplicated (source_system, source_sku) key.
SELECT 'classified_rows_equal_landing_rows',
       (SELECT COUNT(*) FROM raw.orders_landing),
       (SELECT COUNT(*) FROM curated.orders_classified),
       IFF((SELECT COUNT(*) FROM raw.orders_landing) = (SELECT COUNT(*) FROM curated.orders_classified), 'OK', 'FAIL')
UNION ALL
-- 2) SKU map uniqueness (PRIMARY KEY is not enforced in Snowflake)
SELECT 'sku_map_duplicate_keys', 0,
       (SELECT COUNT(*) FROM (SELECT 1 FROM curated.sku_map GROUP BY source_system, source_sku HAVING COUNT(*) > 1)),
       IFF((SELECT COUNT(*) FROM (SELECT 1 FROM curated.sku_map GROUP BY source_system, source_sku HAVING COUNT(*) > 1)) = 0, 'OK', 'FAIL')
UNION ALL
-- 3) reject rate over the last day: a sudden jump usually means a broken upstream export
SELECT 'quarantine_rate_pct_last_day', 1,
       ROUND(100 * (SELECT COUNT(*) FROM curated.orders_quarantine WHERE quarantined_at > DATEADD('day', -1, CURRENT_TIMESTAMP()))
                 / NULLIF((SELECT COUNT(*) FROM raw.orders_landing WHERE ingested_at > DATEADD('day', -1, CURRENT_TIMESTAMP())), 0), 2),
       IFF(COALESCE(100 * (SELECT COUNT(*) FROM curated.orders_quarantine WHERE quarantined_at > DATEADD('day', -1, CURRENT_TIMESTAMP()))
                 / NULLIF((SELECT COUNT(*) FROM raw.orders_landing WHERE ingested_at > DATEADD('day', -1, CURRENT_TIMESTAMP())), 0), 0) <= 1, 'OK', 'WARN')
UNION ALL
-- 4) unmapped SKUs left open too long: someone needs to add mapping rows
SELECT 'oldest_open_unmapped_sku_hours', 48,
       COALESCE((SELECT oldest_open_hours FROM ops.quarantine_summary WHERE reject_reason = 'UNMAPPED_SKU'), 0),
       IFF(COALESCE((SELECT oldest_open_hours FROM ops.quarantine_summary WHERE reject_reason = 'UNMAPPED_SKU'), 0) <= 48, 'OK', 'WARN');

ALTER TASK curated.run_pipeline_checks RESUME;
ALTER TASK curated.merge_order_changes RESUME;
ALTER TASK curated.replay_quarantine   RESUME;
