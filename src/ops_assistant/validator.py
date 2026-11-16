"""Post-generation claim-to-record validation (deterministic Python, no LLM).

Matching "the number appears somewhere in the results" is NOT enough - a correct number
attached to the wrong SKU, period or unit is still a wrong answer. Every numerical claim
must match an evidence record on ALL of:  metric + entity (SKU/material) + cycle + unit +
source record ID + value.

Additional blocks:
  * any number in the prose that no claim accounts for,
  * a claim whose source record ID is not cited in the text,
  * a missing mandatory qualifier (e.g. a capacity exception that the answer omits).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .generator import GeneratedAnswer
from .tools import Evidence

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def _numbers_in_prose(text: str, evidence: list[Evidence]) -> list[float]:
    text = re.sub(r"\[[^\]]*\]", " ", text)                         # citations
    text = re.sub(r"\d{4}-C\d+", " ", text)                          # cycle labels
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", " ", text)  # timestamps
    for entity in sorted({e.entity for e in evidence}, key=len, reverse=True):
        text = text.replace(entity, " ")                             # SKU / material codes
    return [float(m.replace(",", "")) for m in _NUM.findall(text)]


def validate(answer: GeneratedAnswer, evidence: list[Evidence]) -> ValidationResult:
    errors: list[str] = []
    index = {(e.metric, e.entity, e.cycle, e.unit, e.record_id): e for e in evidence}

    for c in answer.claims:
        e = index.get((c.metric, c.entity, c.cycle, c.unit, c.record_id))
        if e is None:
            errors.append(f"unsupported claim: {c.metric} / {c.entity} / {c.cycle} / {c.unit} / {c.record_id}")
        elif not math.isclose(c.value, e.value, abs_tol=1e-9):
            errors.append(f"value mismatch for {c.metric} {c.entity}: claimed {c.value}, record has {e.value}")
        if c.record_id not in answer.text:
            errors.append(f"source record {c.record_id} not cited")
        if c.cycle not in answer.text or c.unit not in answer.text or c.entity not in answer.text:
            errors.append(f"claim {c.metric} {c.entity}: SKU/cycle/unit qualifier missing from text")

    claimed = {round(c.value, 6) for c in answer.claims}
    for n in _numbers_in_prose(answer.text, evidence):
        if round(n, 6) not in claimed:
            errors.append(f"number {n:g} in text is not backed by any claim")

    # Mandatory qualifier: a capacity exception must never be silently dropped.
    for e in evidence:
        if e.metric == "planned_production" and e.qualifiers.get("capacity_exception"):
            if "capacity exception" not in answer.text.lower():
                errors.append(f"capacity exception for {e.entity} omitted from answer")

    # Completeness: every evidence record that was retrieved must be reflected in a claim.
    claimed_keys = {(c.metric, c.entity, c.cycle, c.record_id) for c in answer.claims}
    for e in evidence:
        if (e.metric, e.entity, e.cycle, e.record_id) not in claimed_keys:
            errors.append(f"retrieved {e.metric} for {e.entity} missing from answer")

    return ValidationResult(ok=not errors, errors=errors)
