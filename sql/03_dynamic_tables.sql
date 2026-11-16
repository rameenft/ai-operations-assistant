-- =============================================================================
-- 03  CURATED INVENTORY + REPLENISHMENT VIEWS via DYNAMIC TABLES   (illustrative)
-- Freshness is declared (TARGET_LAG), not hand-scheduled. The assistant's freshness
-- SLA (< 24h for daily planning) is checked against source_ts / refreshed_at.
-- =============================================================================

CREATE OR REPLACE DYNAMIC TABLE curated.finished_goods_position
    TARGET_LAG = '1 hour'
    WAREHOUSE  = PLANNING_WH
AS
SELECT
    i.sku,
    i.cycle,
    i.on_hand,
    i.scheduled_receipts,
    f.forecast_demand,            -- upstream demand forecast (produced outside this repo)
    f.safety_stock,
    i.record_id                   AS inventory_record_id,
    f.record_id                   AS forecast_record_id,
    LEAST(i.source_ts, f.source_ts) AS source_ts
FROM raw.finished_inventory i
JOIN raw.demand_forecast    f USING (sku, cycle);

CREATE OR REPLACE DYNAMIC TABLE curated.material_position
    TARGET_LAG = '1 hour'
    WAREHOUSE  = PLANNING_WH
AS
SELECT
    m.material, m.unit, m.on_hand, m.scheduled_receipts, m.safety_stock,
    m.pack_size, m.min_order_qty, m.lead_time_days,
    m.record_id, m.source_ts
FROM raw.material_inventory m;

-- Daily validated snapshot feeding the independent fallback dashboard.
-- It is NOT a downstream output of the chatbot: if the chatbot is down, this still works.
CREATE OR REPLACE DYNAMIC TABLE serve.daily_validated_snapshot
    TARGET_LAG = '1 day'
    WAREHOUSE  = PLANNING_WH
AS
SELECT CURRENT_TIMESTAMP() AS snapshot_ts, p.*
FROM serve.production_plan p
WHERE p.dq_passed;
