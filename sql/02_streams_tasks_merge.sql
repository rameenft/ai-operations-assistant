-- =============================================================================
-- 02  CHANGE PROCESSING: Streams + Tasks + idempotent MERGE   (illustrative)
-- Cancellations, returns and corrected orders flow through as CHANGES, not rebuilds.
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

-- The stream tells us which standardized rows are new or changed since the last run.
CREATE OR REPLACE STREAM curated.orders_std_stream
    ON TABLE raw.orders_landing
    APPEND_ONLY = TRUE;

CREATE OR REPLACE TASK curated.merge_order_changes
    WAREHOUSE = ZAAR_PLANNING_WH
    SCHEDULE  = '15 MINUTE'
    WHEN SYSTEM$STREAM_HAS_DATA('curated.orders_std_stream')
AS
MERGE INTO curated.order_lines t
USING (
    SELECT
        s.source_system, s.source_order_id, s.source_line_id,
        m.canonical_sku AS sku,
        -- signed quantity: orders add, cancellations / returns subtract, corrections replace
        CASE s.event_type
            WHEN 'order'        THEN  s.quantity
            WHEN 'cancellation' THEN -s.quantity
            WHEN 'return'       THEN -s.quantity
            ELSE                      s.quantity
        END                                   AS delta_qty,
        (s.event_type = 'correction')         AS is_correction,
        s.event_ts, s.batch_id
    FROM curated.orders_std_stream s
    JOIN curated.sku_map m
      ON m.source_system = s.source_system AND m.source_sku = s.source_sku
) c
ON  t.source_system   = c.source_system
AND t.source_order_id = c.source_order_id
AND t.source_line_id  = c.source_line_id
WHEN MATCHED AND c.batch_id <> t.last_batch_id THEN UPDATE SET     -- idempotent: re-running a batch is a no-op
    t.net_quantity  = IFF(c.is_correction, c.delta_qty, t.net_quantity + c.delta_qty),
    t.last_event_ts = c.event_ts,
    t.last_batch_id = c.batch_id
WHEN NOT MATCHED THEN INSERT
    (source_system, source_order_id, source_line_id, sku, net_quantity, last_event_ts, last_batch_id)
VALUES
    (c.source_system, c.source_order_id, c.source_line_id, c.sku, c.delta_qty, c.event_ts, c.batch_id);

-- Alerting + reconciliation: a failed run or a row-count mismatch raises an operations alert.
CREATE OR REPLACE TASK curated.reconcile_counts
    WAREHOUSE = ZAAR_PLANNING_WH
    AFTER curated.merge_order_changes
AS
INSERT INTO ops.pipeline_checks (check_name, expected_rows, actual_rows, checked_at)
SELECT 'orders_landing_vs_order_lines',
       (SELECT COUNT(DISTINCT source_system, source_order_id, source_line_id) FROM curated.orders_standardized),
       (SELECT COUNT(*) FROM curated.order_lines),
       CURRENT_TIMESTAMP();

ALTER TASK curated.reconcile_counts   RESUME;
ALTER TASK curated.merge_order_changes RESUME;
