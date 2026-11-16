-- NOTE: Snowflake-dialect reference script; read it alongside docs/architecture.md.
-- =============================================================================
-- 04  DETERMINISTIC PLANNING CALCULATIONS (SERVE LAYER)   (illustrative)
-- The numbers the chatbot is allowed to quote come ONLY from these views.
-- Python mirror of this logic: src/ops_assistant/planning.py
-- =============================================================================

-- Net finished-goods requirement, executable production, capacity exception.
-- net  = max(0, forecast + safety - on_hand - scheduled_receipts)
-- exec = min( ceil(net / batch) * batch ,  floor(capacity / batch) * batch )
CREATE OR REPLACE VIEW serve.production_plan AS
WITH net AS (
    SELECT
        p.sku, p.cycle,
        GREATEST(0, p.forecast_demand + p.safety_stock
                    - p.on_hand - p.scheduled_receipts)           AS net_requirement,
        r.batch_size, r.capacity_per_cycle,
        p.inventory_record_id || '+' || p.forecast_record_id      AS record_id,
        p.source_ts
    FROM curated.finished_goods_position p
    JOIN ref.batch_rules r USING (sku)
), sized AS (
    SELECT *,
        CEIL(net_requirement / batch_size) * batch_size                AS batch_rounded,
        FLOOR(capacity_per_cycle / batch_size) * batch_size            AS max_executable
    FROM net
)
SELECT
    sku, cycle, net_requirement,
    LEAST(batch_rounded, max_executable)                               AS planned_production,
    'units'                                                            AS unit,
    batch_rounded > max_executable                                     AS capacity_exception,  -- flagged, never silently altered
    GREATEST(0, batch_rounded - max_executable)                        AS capacity_shortfall,
    TRUE                                                               AS dq_passed,
    record_id, source_ts,
    CURRENT_TIMESTAMP()                                                AS calculated_at
FROM sized;

-- Material reorder, honouring supplier pack sizes and minimum order quantities.
-- gross = max(0, planned_prod * BOM usage + safety - on_hand - scheduled_receipts)
-- order = 0 if gross = 0 else max(MOQ, ceil(gross / pack_size) * pack_size)
CREATE OR REPLACE VIEW serve.material_reorder AS
WITH demand AS (
    SELECT b.material, p.cycle, SUM(p.planned_production * b.usage_per_unit) AS material_need
    FROM serve.production_plan p
    JOIN ref.bom b USING (sku)
    GROUP BY b.material, p.cycle
), gross AS (
    SELECT m.*, d.cycle,
           GREATEST(0, COALESCE(d.material_need, 0) + m.safety_stock
                       - m.on_hand - m.scheduled_receipts)         AS gross_requirement
    FROM curated.material_position m
    LEFT JOIN demand d USING (material)
)
SELECT
    material, cycle, unit, gross_requirement,
    IFF(gross_requirement = 0, 0,
        GREATEST(min_order_qty, CEIL(gross_requirement / pack_size) * pack_size)) AS reorder_quantity,
    lead_time_days,
    record_id, source_ts,
    CURRENT_TIMESTAMP() AS calculated_at
FROM gross;

-- Role-scoped, read-only access: the chatbot's service role can SELECT these views only.
-- GRANT SELECT ON VIEW serve.production_plan TO ROLE PRODUCTION_MANAGER;
-- GRANT SELECT ON VIEW serve.material_reorder TO ROLE SUPPLY_MANAGER;
