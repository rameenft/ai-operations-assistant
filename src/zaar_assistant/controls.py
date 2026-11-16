"""Operational controls: freshness SLA, bounded retry with backoff, alert log."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, TypeVar

from .tools import Evidence, TransientToolError

T = TypeVar("T")
FRESHNESS_SLA = timedelta(hours=24)  # daily-planning decision-safe window


@dataclass
class AlertLog:
    events: list[dict] = field(default_factory=list)

    def emit(self, level: str, code: str, detail: str = "") -> None:
        self.events.append({"level": level, "code": code, "detail": detail})

    def codes(self) -> list[str]:
        return [e["code"] for e in self.events]


def stale_records(evidence: list[Evidence], now: datetime, sla: timedelta = FRESHNESS_SLA) -> list[Evidence]:
    return [e for e in evidence if now - datetime.fromisoformat(e.source_ts) > sla]


def with_retry(fn: Callable[[], T], attempts: int = 2, backoff_s: float = 0.0, alerts: AlertLog | None = None) -> T:
    """Bounded retry for transient tool errors only. After the last attempt the error propagates."""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except TransientToolError as exc:
            if alerts:
                alerts.emit("warn", "TOOL_RETRY", f"attempt {i}/{attempts}: {exc}")
            if i == attempts:
                raise
            time.sleep(backoff_s * (2 ** (i - 1)))
    raise AssertionError("unreachable")
