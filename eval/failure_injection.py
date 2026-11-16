"""Failure-injection scenarios: prove the assistant fails CLOSED.

Two groups, mapped to the release gates:
  * accuracy scenarios  - a (simulated) LLM writes a wrong answer; the validator must block it
                          (swapped SKU / period / unit, wrong value, omitted qualifier, invented number)
  * reliability scenarios - tool timeouts, stale data, unauthorised role, validator crash,
                          changed orders (replay), and the V4 batch-size regression
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_assistant.answer import ANSWER, REFUSE, SAFE_FAILURE, answer_question  # noqa: E402
from ops_assistant.db import build_database, refresh_curated  # noqa: E402
from ops_assistant.generator import GeneratedAnswer, TemplateGenerator, fmt  # noqa: E402
from ops_assistant.tools import TransientToolError, run_tool  # noqa: E402

NOW = datetime(2026, 11, 15, 9, 0, 0)  # 4h after the synthetic source timestamps
PM, SM = "production_manager", "supply_manager"


class TamperedGenerator:
    """Wraps the good generator and corrupts it the way a real LLM might."""

    def __init__(self, mode: str, good_on_retry: bool = False):
        self.mode, self.good_on_retry, self.base = mode, good_on_retry, TemplateGenerator()

    def generate(self, question, evidence, attempt=0) -> GeneratedAnswer:
        good = self.base.generate(question, evidence, attempt)
        if self.good_on_retry and attempt > 0:
            return good
        text, claims = good.text, list(good.claims)
        c0 = claims[0]
        if self.mode == "swap_sku":
            other = "SCR-PNT-001" if c0.entity != "SCR-PNT-001" else "SCR-TOP-001"
            text = text.replace(c0.entity, other)
            claims = [replace(c, entity=other) for c in claims]
        elif self.mode == "swap_period":
            text = text.replace(c0.cycle, "2026-C11")
            claims = [replace(c, cycle="2026-C11") for c in claims]
        elif self.mode == "swap_unit":
            text = text.replace(c0.unit, "cases")
            claims = [replace(c, unit="cases") for c in claims]
        elif self.mode == "wrong_value":  # e.g. quoting the un-batched 370 instead of 400
            new = c0.value - 30
            text = re.sub(r"(\):[^\d]*)\d[\d,]*", lambda m: m.group(1) + fmt(new), text, count=1)
            claims[0] = replace(c0, value=new)
        elif self.mode == "omit_qualifier":
            text = re.sub(r" Capacity exception:[^\[]*", "", text)
        elif self.mode == "invented_number":
            text += " Demand is expected to grow 12% next month."
        return GeneratedAnswer(text, claims)


@dataclass
class Result:
    name: str
    gate: str       # "accuracy" | "reliability"
    passed: bool
    detail: str = ""


def _blocked(resp, forbidden: str = "") -> bool:
    return resp.status == SAFE_FAILURE and (not forbidden or forbidden not in resp.text)


def run_accuracy_scenarios() -> list[Result]:
    out = []
    cases = [  # (mode, question) - SCR-PNT-001 for omit_qualifier because it has the capacity exception
        ("swap_sku", "How many scrub tops should we produce?"),
        ("swap_period", "How many scrub tops should we produce?"),
        ("swap_unit", "How many scrub tops should we produce?"),
        ("wrong_value", "How many scrub tops should we produce?"),
        ("omit_qualifier", "How many scrub pants should we make?"),
        ("invented_number", "How many scrub tops should we produce?"),
    ]
    for mode, q in cases:
        conn = build_database()
        r = answer_question(q, PM, conn, generator=TamperedGenerator(mode), now=NOW)
        out.append(Result(f"blocks_{mode}", "accuracy", _blocked(r), f"status={r.status}"))
    conn = build_database()
    r = answer_question("How many scrub tops should we produce?", PM, conn,
                        generator=TamperedGenerator("swap_sku", good_on_retry=True), now=NOW)
    out.append(Result("one_controlled_regeneration_recovers", "accuracy",
                      r.status == ANSWER and "VALIDATION_MISMATCH" in r.alerts.codes(), f"status={r.status}"))
    return out


def run_reliability_scenarios() -> list[Result]:
    out = []

    def flaky(fail_times: int):
        state = {"n": 0}

        def runner(conn, name, params, role):
            state["n"] += 1
            if state["n"] <= fail_times:
                raise TransientToolError("simulated timeout")
            return run_tool(conn, name, params, role)
        return runner

    q = "How many scrub tops should we produce?"
    r = answer_question(q, PM, build_database(), tool_runner=flaky(1), now=NOW)
    out.append(Result("tool_timeout_recovers_within_retry_budget", "reliability",
                      r.status == ANSWER and "TOOL_RETRY" in r.alerts.codes(), f"status={r.status}"))

    r = answer_question(q, PM, build_database(), tool_runner=flaky(99), now=NOW)
    out.append(Result("tool_timeout_exhausts_retries_then_safe_failure", "reliability",
                      _blocked(r, "400") and "TOOL_FAILED" in r.alerts.codes(), f"status={r.status}"))

    r = answer_question(q, PM, build_database(), now=datetime(2026, 11, 18, 9, 0, 0))
    out.append(Result("stale_data_blocks_recommendation", "reliability",
                      _blocked(r, "400") and "2026-11-15T05:00:00" in r.text, f"status={r.status}"))

    r = answer_question(q, SM, build_database(), now=NOW)
    out.append(Result("unauthorised_role_refused_without_leakage", "reliability",
                      r.status == REFUSE and not r.evidence and "400" not in r.text, f"status={r.status}"))

    def boom(*_a, **_k):
        raise RuntimeError("validator crashed")
    r = answer_question(q, PM, build_database(), validator=boom, now=NOW)
    out.append(Result("validator_exception_fails_closed", "reliability",
                      _blocked(r, "400") and "PIPELINE_EXCEPTION" in r.alerts.codes(), f"status={r.status}"))

    # Replay: an order change lowers on-hand stock; the assistant must reflect the NEW approved quantity.
    conn = build_database()
    conn.execute("UPDATE finished_inventory SET on_hand = 130 WHERE sku = 'SCR-TOP-001'")
    refresh_curated(conn)
    r = answer_question(q, PM, conn, now=NOW)
    ok = r.status == ANSWER and any(e.metric == "planned_production" and e.value == 500 for e in r.evidence)
    out.append(Result("changed_order_replay_updates_answer", "reliability", ok, f"status={r.status}"))

    # V4 regression: 370 is mathematically right but not executable with 100-unit batches.
    r = answer_question(q, PM, build_database(), now=NOW)
    planned = next(e.value for e in r.evidence if e.metric == "planned_production")
    out.append(Result("v4_regression_batch_rounding_370_to_400", "reliability",
                      planned == 400 and "planned production 370" not in r.text, f"planned={planned}"))
    return out
