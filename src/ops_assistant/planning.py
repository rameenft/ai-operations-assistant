"""Deterministic planning calculations.

Python mirror of sql/04_planning_views.sql. The chatbot never computes these numbers;
it only retrieves and explains the approved results.

    net requirement  = max(0, forecast + safety - on_hand - scheduled_receipts)
    executable prod  = round net UP to a whole batch, then cap at what capacity allows
                       (whole batches only). If the cap bites, raise a capacity exception
                       instead of silently changing the recommendation.
    material reorder = max(0, planned_prod * BOM usage + safety - on_hand - receipts),
                       rounded up to supplier pack size and at least the MOQ.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ProductionPlan:
    net_requirement: int
    batch_rounded: int
    planned_production: int
    capacity_exception: bool
    capacity_shortfall: int


@dataclass(frozen=True)
class MaterialPlan:
    gross_requirement: float
    reorder_quantity: float


def net_requirement(forecast: int, safety: int, on_hand: int, receipts: int) -> int:
    return max(0, forecast + safety - on_hand - receipts)


def round_up_to_batch(qty: int, batch_size: int) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    return math.ceil(qty / batch_size) * batch_size


def plan_production(
    forecast: int, safety: int, on_hand: int, receipts: int, batch_size: int, capacity: int
) -> ProductionPlan:
    net = net_requirement(forecast, safety, on_hand, receipts)
    rounded = round_up_to_batch(net, batch_size)
    max_executable = (capacity // batch_size) * batch_size  # whole batches only
    planned = min(rounded, max_executable)
    return ProductionPlan(
        net_requirement=net,
        batch_rounded=rounded,
        planned_production=planned,
        capacity_exception=rounded > max_executable,
        capacity_shortfall=max(0, rounded - max_executable),
    )


def plan_material(
    material_need: float, safety: float, on_hand: float, receipts: float, pack_size: float, moq: float
) -> MaterialPlan:
    gross = round(max(0.0, material_need + safety - on_hand - receipts), 6)
    if gross == 0:
        return MaterialPlan(0, 0)
    packs = math.ceil(round(gross / pack_size, 9))
    return MaterialPlan(_tidy(gross), _tidy(max(moq, packs * pack_size)))


def _tidy(x: float):
    return int(x) if float(x).is_integer() else round(x, 2)
