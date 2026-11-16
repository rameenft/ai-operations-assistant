"""Answer generation.

In production a Cortex LLM writes the concise answer from the structured evidence and also
returns the claims it made. Here `TemplateGenerator` is a deterministic stand-in so the
evaluation is reproducible; the *validator* treats any generator as untrusted, which is the point.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .tools import Evidence


@dataclass(frozen=True)
class Claim:
    metric: str
    entity: str
    cycle: str
    value: float
    unit: str
    record_id: str


@dataclass(frozen=True)
class GeneratedAnswer:
    text: str
    claims: list[Claim]


class Generator(Protocol):
    def generate(self, question: str, evidence: list[Evidence], attempt: int = 0) -> GeneratedAnswer: ...


def fmt(v: float) -> str:
    return f"{int(v):,}" if float(v).is_integer() else f"{v:,.2f}"


# Format agreed with the managers after trial use: lead with the number they asked for, keep
# supporting figures to short bullets, and never run past a few lines (compact one-liners for lists).
_HEADLINE = {
    "planned_production": "produce {v} {u}",
    "reorder_quantity": "reorder {v} {u}",
    "safety_stock": "safety stock {v} {u}",
    "on_hand": "{v} {u} on hand",
    "net_requirement": "net requirement {v} {u}",
    "lead_time_days": "lead time {v} {u}",
}
_LABEL = {
    "net_requirement": "Net requirement",
    "planned_production": "Planned production",
    "on_hand": "On hand",
    "scheduled_receipts": "Incoming",
    "reorder_quantity": "Reorder",
    "lead_time_days": "Supplier lead time",
    "safety_stock": "Safety stock",
}
_PRIORITY = ["planned_production", "reorder_quantity", "safety_stock", "on_hand", "net_requirement",
             "lead_time_days", "scheduled_receipts"]
COMPACT_ABOVE = 2  # more entities than this -> one line each instead of headline + bullets


def _headline_metric(items: list[Evidence]) -> Evidence:
    by_metric = {e.metric: e for e in items}
    return by_metric[next(m for m in _PRIORITY if m in by_metric)]


class TemplateGenerator:
    def generate(self, question: str, evidence: list[Evidence], attempt: int = 0) -> GeneratedAnswer:
        groups: dict[tuple, list[Evidence]] = {}
        for e in evidence:
            groups.setdefault((e.entity, e.cycle), []).append(e)
        compact = len(groups) > COMPACT_ABOVE

        lines, claims = [], []
        for (entity, cycle), items in groups.items():
            claims += [Claim(e.metric, e.entity, e.cycle, e.value, e.unit, e.record_id) for e in items]
            rec_ids = ", ".join(dict.fromkeys(e.record_id for e in items))
            head = _headline_metric(items)
            headline = _HEADLINE[head.metric].format(v=fmt(head.value), u=head.unit)
            shortfall = next((e for e in items if e.metric == "capacity_shortfall"), None)
            planned = next((e for e in items if e.metric == "planned_production"), None)
            rest = [e for e in items if e is not head and e.metric != "capacity_shortfall"]
            warn = (f"Capacity exception: requirement exceeds cycle capacity by {fmt(shortfall.value)} "
                    f"{shortfall.unit}, so production is capped at {fmt(planned.value)} {planned.unit}."
                    if shortfall and planned else "")

            if compact:
                extra = "".join(f"; {_LABEL[e.metric].lower()} {fmt(e.value)} {e.unit}" for e in rest)
                lines.append(f"- **{entity}** ({cycle}): {headline}{extra} [{rec_ids}]" + (f"  \n  {warn}" if warn else ""))
            else:
                lines.append(f"**{entity} ({cycle}): {headline}** [{rec_ids}]")
                lines += [f"- {_LABEL[e.metric]}: {fmt(e.value)} {e.unit}" for e in rest]
                if warn:
                    lines.append(f"- {warn}")
        return GeneratedAnswer("\n".join(lines), claims)
