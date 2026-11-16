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


_PHRASES = {
    "net_requirement": "net requirement {v} {u}",
    "planned_production": "planned production {v} {u}",
    "on_hand": "{v} {u} on hand",
    "scheduled_receipts": "{v} {u} scheduled to arrive",
    "reorder_quantity": "reorder {v} {u}",
    "lead_time_days": "supplier lead time {v} {u}",
}


class TemplateGenerator:
    def generate(self, question: str, evidence: list[Evidence], attempt: int = 0) -> GeneratedAnswer:
        groups: dict[tuple, list[Evidence]] = {}
        for e in evidence:
            groups.setdefault((e.entity, e.cycle), []).append(e)

        lines, claims = [], []
        for (entity, cycle), items in groups.items():
            parts, extra = [], ""
            for e in items:
                claims.append(Claim(e.metric, e.entity, e.cycle, e.value, e.unit, e.record_id))
                if e.metric == "capacity_shortfall":
                    planned = next(x for x in items if x.metric == "planned_production")
                    extra = (f" Capacity exception: the batch-rounded requirement exceeds cycle capacity "
                             f"by {fmt(e.value)} {e.unit}, so planned production is capped at "
                             f"{fmt(planned.value)} {planned.unit}.")
                else:
                    parts.append(_PHRASES[e.metric].format(v=fmt(e.value), u=e.unit))
            rec_ids = ", ".join(dict.fromkeys(e.record_id for e in items))
            lines.append(f"{entity} ({cycle}): {', '.join(parts)}.{extra} [{rec_ids}]")
        return GeneratedAnswer("\n".join(lines), claims)
