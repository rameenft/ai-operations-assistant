import sqlite3

import pytest

from ops_assistant.ingest import (load_ingest_tables, quarantine_summary, reconcile, replay_unmapped_skus,
                                   run_ingest, sku_map_duplicates)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    load_ingest_tables(c)
    return c


def _lines(c):
    return {(r["source_order_id"], r["source_line_id"]): r["net_quantity"] for r in c.execute("SELECT * FROM order_lines")}


def test_every_bad_row_is_quarantined_with_its_reason_not_dropped(conn):
    result = run_ingest(conn)
    assert result == {"applied": 5, "duplicates": 1, "quarantined": 7}
    assert quarantine_summary(conn) == {
        "MISSING_KEY": 1, "MISSING_SKU": 1, "MISSING_TIMESTAMP": 1, "NEGATIVE_QUANTITY": 1,
        "NULL_QUANTITY": 1, "UNKNOWN_EVENT_TYPE": 1, "UNMAPPED_SKU": 1,
    }


def test_unmapped_sku_survives_the_left_join_with_null_canonical_sku(conn):
    row = conn.execute("SELECT canonical_sku, reject_reason FROM orders_classified WHERE source_sku='SHP-NEWITEM-XYZ'").fetchone()
    assert row["canonical_sku"] is None and row["reject_reason"] == "UNMAPPED_SKU"
    # an INNER JOIN would have silently lost it (and the blank-SKU row): the failure LEFT JOIN avoids
    inner = conn.execute("""SELECT COUNT(*) FROM orders_landing l JOIN sku_map m
                            ON m.source_system=l.source_system AND m.source_sku=l.source_sku""").fetchone()[0]
    landing = conn.execute("SELECT COUNT(*) FROM orders_landing").fetchone()[0]
    assert landing == 13 and inner == landing - 2


def test_valid_rows_only_reach_order_lines_and_cancellation_nets_out(conn):
    run_ingest(conn)
    lines = _lines(conn)
    assert lines[("1001", "1")] == 7          # 10 ordered (duplicate row ignored) - 3 cancelled
    assert lines[("1001", "2")] == 5 and lines[("H-77", "1")] == 100 and lines[("1002", "1")] == 2
    assert ("1003", "1") not in lines         # unmapped SKU never reaches planning data


def test_reconciliation_accounts_for_every_landed_row(conn):
    run_ingest(conn)
    r = reconcile(conn)
    assert r["ok"] and r["unaccounted"] == 0
    assert r["landing"] == r["applied"] + r["duplicate"] + r["quarantined"] == 13


def test_ingest_is_idempotent(conn):
    run_ingest(conn)
    before = (_lines(conn), reconcile(conn))
    assert run_ingest(conn) == {"applied": 0, "duplicates": 0, "quarantined": 0}
    assert (_lines(conn), reconcile(conn)) == before


def test_fixing_the_sku_map_and_replaying_recovers_the_row(conn):
    run_ingest(conn)
    assert replay_unmapped_skus(conn) == 0    # map not fixed yet: nothing to replay
    conn.execute("INSERT INTO sku_map VALUES ('shopify', 'SHP-NEWITEM-XYZ', 'SCR-TOP-001', 1)")
    assert replay_unmapped_skus(conn) == 1
    assert run_ingest(conn)["applied"] == 1
    assert _lines(conn)[("1003", "1")] == 4
    assert "UNMAPPED_SKU" not in quarantine_summary(conn)
    assert quarantine_summary(conn)["MISSING_KEY"] == 1   # malformed rows stay OPEN: a map change can't fix them
    assert reconcile(conn)["ok"]                          # original row QUARANTINED, replay copy APPLIED


def test_duplicate_sku_map_key_is_detected_because_it_fans_out_the_left_join(conn):
    assert sku_map_duplicates(conn) == []
    conn.execute("INSERT INTO sku_map VALUES ('shopify', 'SHP-SCRTOP-BLU-M', 'SCR-TOP-001', 1)")
    assert sku_map_duplicates(conn) == [("shopify", "SHP-SCRTOP-BLU-M")]
    landing = conn.execute("SELECT COUNT(*) FROM orders_landing").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM orders_classified").fetchone()[0] > landing
    assert not reconcile(conn)["ok"]                      # the reconciliation check catches it
