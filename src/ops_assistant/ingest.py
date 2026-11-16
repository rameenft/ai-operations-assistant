"""Runnable mirror of sql/01 and sql/02: classify landed orders, quarantine rejects, replay fixes.

Design rule: nothing is rejected at the door and nothing is dropped. Every landed row is
classified exactly once, so a row always ends in one of three places:

    APPLIED       valid, merged into order_lines
    DUPLICATE     valid but a repeat of an event already in the same run (deduped)
    QUARANTINED   rejected, with a reason, waiting for a fix

    landing rows == applied + duplicates + quarantined      (see reconcile)

Why LEFT JOIN: an INNER JOIN to the SKU map silently drops unmapped rows, which are exactly the
rows that need attention. With a LEFT JOIN they survive with canonical_sku NULL and get a reason.
Why exactly-once: in Snowflake the Stream offset gives this; here the `ingest_outcome` table does.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .db import DATA_DIR, _load_csv

LANDING_COLS = ["landing_id", "source_system", "source_order_id", "source_line_id", "source_sku",
                "quantity", "event_type", "event_ts", "batch_id"]
VALID_EVENTS = ("order", "cancellation", "return", "correction")

_CLASSIFIED_VIEW = """
CREATE VIEW orders_classified AS
SELECT l.*, m.canonical_sku,
  CASE
    WHEN TRIM(COALESCE(CAST(l.source_order_id AS TEXT), '')) = ''
      OR TRIM(COALESCE(CAST(l.source_line_id  AS TEXT), '')) = ''   THEN 'MISSING_KEY'
    WHEN TRIM(COALESCE(l.source_sku, '')) = ''                      THEN 'MISSING_SKU'
    WHEN m.canonical_sku IS NULL                                    THEN 'UNMAPPED_SKU'
    WHEN l.quantity IS NULL                                         THEN 'NULL_QUANTITY'
    WHEN l.quantity < 0                                             THEN 'NEGATIVE_QUANTITY'
    WHEN l.event_type IS NULL
      OR l.event_type NOT IN ('order', 'cancellation', 'return', 'correction')
                                                                    THEN 'UNKNOWN_EVENT_TYPE'
    WHEN l.event_ts IS NULL                                         THEN 'MISSING_TIMESTAMP'
  END AS reject_reason
FROM orders_landing l
LEFT JOIN sku_map m
       ON m.source_system = l.source_system AND m.source_sku = l.source_sku AND m.is_active = 1
"""


def load_ingest_tables(conn: sqlite3.Connection, data_dir: Path = DATA_DIR) -> None:
    conn.execute("DROP VIEW IF EXISTS orders_classified")
    _load_csv(conn, Path(data_dir) / "orders_landing.csv", "orders_landing")
    _load_csv(conn, Path(data_dir) / "sku_map.csv", "sku_map")
    for col in LANDING_COLS:  # an empty CSV cell is a NULL, like an empty Sheets cell in the real feed
        conn.execute(f"UPDATE orders_landing SET {col} = NULL WHERE {col} = ''")
    conn.executescript(
        """
        DROP TABLE IF EXISTS orders_quarantine;
        DROP TABLE IF EXISTS ingest_outcome;
        DROP TABLE IF EXISTS order_lines;
        CREATE TABLE orders_quarantine (
            quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT, landing_id INTEGER, reject_reason TEXT,
            status TEXT DEFAULT 'OPEN', quarantined_at TEXT, resolved_at TEXT);
        CREATE TABLE ingest_outcome (landing_id INTEGER PRIMARY KEY, outcome TEXT);
        CREATE TABLE order_lines (
            source_system, source_order_id, source_line_id, sku, net_quantity, last_batch_id,
            PRIMARY KEY (source_system, source_order_id, source_line_id));
        """
    )
    conn.execute(_CLASSIFIED_VIEW)
    conn.commit()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_ingest(conn: sqlite3.Connection) -> dict:
    """Process landing rows that have no outcome yet. Safe to call repeatedly (exactly-once)."""
    rows = conn.execute(
        """SELECT c.* FROM orders_classified c
           LEFT JOIN ingest_outcome o ON o.landing_id = c.landing_id
           WHERE o.landing_id IS NULL ORDER BY c.landing_id"""
    ).fetchall()

    quarantined = [r for r in rows if r["reject_reason"]]
    valid = [r for r in rows if not r["reject_reason"]]

    for r in quarantined:  # 1) rejects -> quarantine with reason
        conn.execute("INSERT INTO orders_quarantine (landing_id, reject_reason, quarantined_at) VALUES (?,?,?)",
                     (r["landing_id"], r["reject_reason"], _now()))
        conn.execute("INSERT INTO ingest_outcome VALUES (?, 'QUARANTINED')", (r["landing_id"],))

    kept: dict[tuple, sqlite3.Row] = {}  # 2) dedupe on the natural key of an event, latest row wins
    for r in valid:
        kept[(r["source_system"], str(r["source_order_id"]), str(r["source_line_id"]),
              r["event_type"], r["batch_id"])] = r
    kept_ids = {r["landing_id"] for r in kept.values()}
    duplicates = [r for r in valid if r["landing_id"] not in kept_ids]
    for r in duplicates:
        conn.execute("INSERT INTO ingest_outcome VALUES (?, 'DUPLICATE')", (r["landing_id"],))

    for r in sorted(kept.values(), key=lambda x: x["landing_id"]):  # 3) apply to order_lines
        key = (r["source_system"], str(r["source_order_id"]), str(r["source_line_id"]))
        existing = conn.execute(
            "SELECT net_quantity FROM order_lines WHERE source_system=? AND source_order_id=? AND source_line_id=?",
            key).fetchone()
        sign = {"order": 1, "cancellation": -1, "return": -1}.get(r["event_type"], 0)
        if r["event_type"] == "correction":
            qty = r["quantity"]
        else:
            qty = (existing["net_quantity"] if existing else 0) + sign * r["quantity"]
        if existing:
            conn.execute("""UPDATE order_lines SET net_quantity=?, last_batch_id=?
                            WHERE source_system=? AND source_order_id=? AND source_line_id=?""",
                         (qty, r["batch_id"], *key))
        else:
            conn.execute("INSERT INTO order_lines VALUES (?,?,?,?,?,?)",
                         (*key, r["canonical_sku"], qty, r["batch_id"]))
        conn.execute("INSERT INTO ingest_outcome VALUES (?, 'APPLIED')", (r["landing_id"],))

    conn.commit()
    return {"applied": len(kept), "duplicates": len(duplicates), "quarantined": len(quarantined)}


def replay_unmapped_skus(conn: sqlite3.Connection) -> int:
    """After the SKU map is fixed, re-land UNMAPPED_SKU rows as a NEW batch (RAW stays immutable)."""
    fixable = conn.execute(
        """SELECT q.quarantine_id, l.* FROM orders_quarantine q
           JOIN orders_landing l ON l.landing_id = q.landing_id
           JOIN sku_map m ON m.source_system = l.source_system AND m.source_sku = l.source_sku AND m.is_active = 1
           WHERE q.status = 'OPEN' AND q.reject_reason = 'UNMAPPED_SKU'"""
    ).fetchall()
    batch = "REPLAY-" + datetime.now().strftime("%Y%m%d%H%M%S")
    next_id = conn.execute("SELECT COALESCE(MAX(landing_id), 0) + 1 FROM orders_landing").fetchone()[0]
    for i, r in enumerate(fixable):
        conn.execute("INSERT INTO orders_landing VALUES (?,?,?,?,?,?,?,?,?)",
                     (next_id + i, r["source_system"], r["source_order_id"], r["source_line_id"],
                      r["source_sku"], r["quantity"], r["event_type"], r["event_ts"], batch))
        conn.execute("UPDATE orders_quarantine SET status='RESOLVED', resolved_at=? WHERE quarantine_id=?",
                     (_now(), r["quarantine_id"]))
    conn.commit()
    return len(fixable)


def quarantine_summary(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["reject_reason"]: r["n"] for r in conn.execute(
        "SELECT reject_reason, COUNT(*) AS n FROM orders_quarantine WHERE status='OPEN' GROUP BY 1 ORDER BY 1")}


def sku_map_duplicates(conn: sqlite3.Connection) -> list[tuple]:
    """Uniqueness is not enforced by the database, and a duplicated key fans the LEFT JOIN out."""
    return [tuple(r) for r in conn.execute(
        "SELECT source_system, source_sku FROM sku_map GROUP BY 1, 2 HAVING COUNT(*) > 1")]


def reconcile(conn: sqlite3.Connection) -> dict:
    landing = conn.execute("SELECT COUNT(*) FROM orders_landing").fetchone()[0]
    classified = conn.execute("SELECT COUNT(*) FROM orders_classified").fetchone()[0]
    outcomes = {r["outcome"]: r["n"] for r in conn.execute(
        "SELECT outcome, COUNT(*) AS n FROM ingest_outcome GROUP BY outcome")}
    accounted = sum(outcomes.values())
    return {"landing": landing, "classified": classified, **{k.lower(): v for k, v in outcomes.items()},
            "unaccounted": landing - accounted, "ok": landing == classified == accounted}
