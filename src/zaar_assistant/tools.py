"""Allowlisted, parameterized, read-only retrieval tools.

The model (or here, the router) can only *pick* from this registry and supply parameters.
It never writes SQL and never gets arbitrary execution rights. The server side:
  1. checks the caller's role is allowed to use the tool (RBAC),
  2. validates parameters against the catalog of known SKUs / materials / cycles,
  3. runs a fixed, parameterized SELECT against the curated serve tables,
  4. returns structured Evidence (metric, entity, cycle, value, unit, record ID, source timestamp).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


class ToolError(Exception):
    """Base class for tool failures."""


class ParameterError(ToolError):
    """Bad / missing / unknown parameter -> ask the user to clarify."""


class AuthorizationError(ToolError):
    """Role is not allowed to use this tool or data domain -> refuse, leak nothing."""


class TransientToolError(ToolError):
    """Timeout or transient failure -> eligible for bounded retry."""


@dataclass(frozen=True)
class Evidence:
    metric: str
    entity: str
    cycle: str
    value: float
    unit: str
    record_id: str
    source_ts: str
    qualifiers: dict = field(default_factory=dict)


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "production_manager": {"get_production_plan", "get_inventory_status"},
    "supply_manager": {"get_material_reorder"},
}


def _require_known(conn, table: str, col: str, value, label: str) -> None:
    if not isinstance(value, str) or conn.execute(
        f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (value,)
    ).fetchone() is None:
        raise ParameterError(f"Unknown {label}: {value!r}")


def _require_cycle(conn, cycle) -> None:
    _require_known(conn, "production_plan", "cycle", cycle, "planning cycle")


def get_production_plan(conn: sqlite3.Connection, sku: str, cycle: str) -> list[Evidence]:
    _require_known(conn, "batch_rules", "sku", sku, "SKU")
    _require_cycle(conn, cycle)
    r = conn.execute("SELECT * FROM production_plan WHERE sku = ? AND cycle = ?", (sku, cycle)).fetchone()
    if r is None:
        raise ParameterError(f"No approved plan for {sku} in {cycle}")
    base = dict(entity=sku, cycle=cycle, unit="units", record_id=r["record_id"], source_ts=r["source_ts"])
    out = [
        Evidence(metric="net_requirement", value=r["net_requirement"], **base),
        Evidence(
            metric="planned_production", value=r["planned_production"],
            qualifiers={"capacity_exception": bool(r["capacity_exception"]),
                        "batch_size": r["batch_size"]},
            **base,
        ),
    ]
    if r["capacity_exception"]:
        out.append(Evidence(metric="capacity_shortfall", value=r["capacity_shortfall"], **base))
    return out


def get_inventory_status(conn: sqlite3.Connection, sku: str, cycle: str) -> list[Evidence]:
    _require_known(conn, "batch_rules", "sku", sku, "SKU")
    _require_cycle(conn, cycle)
    r = conn.execute(
        "SELECT * FROM finished_inventory WHERE sku = ? AND cycle = ?", (sku, cycle)
    ).fetchone()
    if r is None:
        raise ParameterError(f"No inventory record for {sku} in {cycle}")
    base = dict(entity=sku, cycle=cycle, unit="units", record_id=r["record_id"], source_ts=r["source_ts"])
    return [
        Evidence(metric="on_hand", value=r["on_hand"], **base),
        Evidence(metric="scheduled_receipts", value=r["scheduled_receipts"], **base),
    ]


def get_material_reorder(conn: sqlite3.Connection, material: str, cycle: str) -> list[Evidence]:
    _require_known(conn, "material_inventory", "material", material, "material")
    _require_cycle(conn, cycle)
    r = conn.execute(
        "SELECT * FROM material_reorder WHERE material = ? AND cycle = ?", (material, cycle)
    ).fetchone()
    if r is None:
        raise ParameterError(f"No reorder record for {material} in {cycle}")
    base = dict(entity=material, cycle=cycle, record_id=r["record_id"], source_ts=r["source_ts"])
    return [
        Evidence(metric="reorder_quantity", value=r["reorder_quantity"], unit=r["unit"], **base),
        Evidence(metric="lead_time_days", value=r["lead_time_days"], unit="days", **base),
    ]


# name -> (callable, exact set of parameter names the tool accepts)
TOOLS = {
    "get_production_plan": (get_production_plan, {"sku", "cycle"}),
    "get_inventory_status": (get_inventory_status, {"sku", "cycle"}),
    "get_material_reorder": (get_material_reorder, {"material", "cycle"}),
}


def run_tool(conn: sqlite3.Connection, name: str, params: dict, role: str) -> list[Evidence]:
    if name not in TOOLS:
        raise ParameterError(f"Tool {name!r} is not allowlisted")
    if name not in ROLE_PERMISSIONS.get(role, set()):
        raise AuthorizationError(f"Role {role!r} may not use {name}")  # checked BEFORE touching data
    fn, allowed = TOOLS[name]
    if set(params) != allowed:
        raise ParameterError(f"{name} requires exactly {sorted(allowed)}")
    return fn(conn, **params)
