"""Local stand-in for the Snowflake serve layer (sqlite, in-memory, synthetic CSVs).

`raw` tables are loaded from CSV; `production_plan` and `material_reorder` are the curated
"views" the assistant is allowed to read - computed with planning.py (see sql/04_planning_views.sql).
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "synthetic"

_CSV_TABLES = {
    "forecast.csv": "forecast",
    "finished_inventory.csv": "finished_inventory",
    "batch_rules.csv": "batch_rules",
    "bom.csv": "bom",
    "material_inventory.csv": "material_inventory",
}


def _coerce(v: str):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def _load_csv(conn: sqlite3.Connection, path: Path, table: str) -> None:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    cols = list(rows[0].keys())
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
    conn.executemany(
        f"INSERT INTO {table} VALUES ({', '.join('?' for _ in cols)})",
        [[_coerce(r[c]) for c in cols] for r in rows],
    )


def build_database(data_dir: Path = DATA_DIR) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for fname, table in _CSV_TABLES.items():
        _load_csv(conn, Path(data_dir) / fname, table)
    refresh_curated(conn)
    return conn


def refresh_curated(conn: sqlite3.Connection) -> None:
    """Recompute the curated planning tables from raw inputs (what Dynamic Tables do in Snowflake)."""
    from .planning import plan_material, plan_production

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("DROP TABLE IF EXISTS production_plan")
    conn.execute("DROP TABLE IF EXISTS material_reorder")
    conn.execute(
        """CREATE TABLE production_plan (
               sku, cycle, net_requirement, planned_production, unit, capacity_exception,
               capacity_shortfall, batch_size, record_id, source_ts, calculated_at)"""
    )
    conn.execute(
        """CREATE TABLE material_reorder (
               material, cycle, unit, gross_requirement, reorder_quantity, lead_time_days,
               record_id, source_ts, calculated_at)"""
    )

    plans: dict[tuple[str, str], int] = {}
    rows = conn.execute(
        """SELECT f.sku, f.cycle, f.forecast_demand, f.safety_stock, f.record_id AS fc_id,
                  f.source_ts AS fc_ts, i.on_hand, i.scheduled_receipts, i.record_id AS inv_id,
                  i.source_ts AS inv_ts, r.batch_size, r.capacity_per_cycle
           FROM forecast f
           JOIN finished_inventory i ON i.sku = f.sku AND i.cycle = f.cycle
           JOIN batch_rules r ON r.sku = f.sku"""
    ).fetchall()
    for r in rows:
        p = plan_production(
            r["forecast_demand"], r["safety_stock"], r["on_hand"], r["scheduled_receipts"],
            r["batch_size"], r["capacity_per_cycle"],
        )
        plans[(r["sku"], r["cycle"])] = p.planned_production
        conn.execute(
            "INSERT INTO production_plan VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r["sku"], r["cycle"], p.net_requirement, p.planned_production, "units",
             int(p.capacity_exception), p.capacity_shortfall, r["batch_size"],
             f"{r['inv_id']}+{r['fc_id']}", min(r["fc_ts"], r["inv_ts"]), now),
        )

    cycles = sorted({c for (_, c) in plans})
    bom = conn.execute("SELECT sku, material, usage_per_unit FROM bom").fetchall()
    for m in conn.execute("SELECT * FROM material_inventory").fetchall():
        for cycle in cycles:
            need = sum(
                plans.get((b["sku"], cycle), 0) * b["usage_per_unit"]
                for b in bom if b["material"] == m["material"]
            )
            mp = plan_material(
                need, m["safety_stock"], m["on_hand"], m["scheduled_receipts"],
                m["pack_size"], m["min_order_qty"],
            )
            conn.execute(
                "INSERT INTO material_reorder VALUES (?,?,?,?,?,?,?,?,?)",
                (m["material"], cycle, m["unit"], mp.gross_requirement, mp.reorder_quantity,
                 m["lead_time_days"], m["record_id"], m["source_ts"], now),
            )
    conn.commit()


def load_catalog(conn: sqlite3.Connection) -> dict:
    """Known entities + default planning cycle, used to validate and normalise request parameters."""
    cycles = sorted(r[0] for r in conn.execute("SELECT DISTINCT cycle FROM production_plan"))
    return {
        "skus": {r[0] for r in conn.execute("SELECT sku FROM batch_rules")},
        "materials": {r[0] for r in conn.execute("SELECT material FROM material_inventory")},
        "cycles": set(cycles),
        "default_cycle": cycles[-1],
    }
