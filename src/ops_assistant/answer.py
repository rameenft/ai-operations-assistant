"""Answer control: ties routing, retrieval, generation and validation into one fail-closed path.

    route -> (clarify | refuse) | retrieve (RBAC, retry) -> freshness gate
          -> generate -> validate (1 controlled regeneration) -> answer | partial | safe_failure

Only validated text is ever returned. Any unexpected exception in the pipeline fails closed.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .controls import AlertLog, stale_records, with_retry
from .db import load_catalog
from .generator import Generator, TemplateGenerator
from .router import route
from .tools import AuthorizationError, Evidence, ParameterError, TransientToolError, run_tool
from .validator import validate

ANSWER, PARTIAL, REFUSE, CLARIFY, SAFE_FAILURE = "answer", "partial", "refuse", "clarify", "safe_failure"


@dataclass
class Response:
    status: str
    text: str
    evidence: list[Evidence] = field(default_factory=list)
    scope: str = ""
    reason: str = ""
    alerts: AlertLog = field(default_factory=AlertLog)


def answer_question(
    question: str,
    role: str,
    conn: sqlite3.Connection,
    *,
    generator: Generator | None = None,
    tool_runner: Callable = run_tool,
    validator: Callable = validate,
    now: datetime | None = None,
    max_regenerations: int = 1,
    retry_attempts: int = 2,
    backoff_s: float = 0.0,
) -> Response:
    generator = generator or TemplateGenerator()
    now = now or datetime.now()
    alerts = AlertLog()

    def done(status: str, text: str, scope: str = "", reason: str = "", evidence=None) -> Response:
        return Response(status, text, evidence or [], scope, reason, alerts)

    try:
        plan = route(question, load_catalog(conn))
        if plan.action == "refuse":
            return done(REFUSE, plan.message, plan.scope, "out_of_scope")
        if plan.action == "clarify":
            return done(CLARIFY, plan.message, plan.scope, "needs_clarification")

        evidence: list[Evidence] = []
        for tool, params in plan.calls:
            try:
                evidence += with_retry(lambda: tool_runner(conn, tool, params, role),
                                       retry_attempts, backoff_s, alerts)
            except AuthorizationError as exc:
                alerts.emit("info", "UNAUTHORIZED_SCOPE", str(exc))
                return done(REFUSE, "You don't have access to that data domain with your current role.",
                            plan.scope, "unauthorized")
            except ParameterError as exc:
                return done(CLARIFY, f"I couldn't use those parameters: {exc}.", plan.scope, "bad_parameters")
            except TransientToolError as exc:
                alerts.emit("error", "TOOL_FAILED", str(exc))
                return done(SAFE_FAILURE, "I couldn't retrieve the data right now. Please try again shortly; "
                            "no figures were produced.", plan.scope, "tool_failure")

        stale = stale_records(evidence, now)
        if stale:
            oldest = min(e.source_ts for e in stale)
            alerts.emit("error", "STALE_INPUTS", f"oldest source_ts {oldest}")
            return done(SAFE_FAILURE, f"The underlying data is stale (last updated {oldest}). "
                        "Recommendations are blocked until the upstream refresh completes.",
                        plan.scope, "stale_data")

        for attempt in range(max_regenerations + 1):
            generated = generator.generate(question, evidence, attempt)
            result = validator(generated, evidence)
            if result.ok:
                break
            alerts.emit("warn", "VALIDATION_MISMATCH", "; ".join(result.errors))
        else:
            alerts.emit("error", "VALIDATION_BLOCKED", "answer withheld")
            return done(SAFE_FAILURE, "I couldn't verify that answer against the source records, so I'm "
                        "not showing it.", plan.scope, "validation_failed")

        as_of = min(e.source_ts for e in evidence)
        text = f"{generated.text}\n\nSource data as of {as_of}."
        if plan.not_covered:
            text += "\n\nNot covered: " + "; ".join(plan.not_covered) + "."
            return done(PARTIAL, text, plan.scope, "partial", evidence)
        return done(ANSWER, text, plan.scope, "", evidence)

    except Exception as exc:  # fail closed, never surface unvalidated output
        alerts.emit("error", "PIPELINE_EXCEPTION", repr(exc))
        return done(SAFE_FAILURE, "Something went wrong and I can't safely answer. The issue has been logged.",
                    reason="exception")
