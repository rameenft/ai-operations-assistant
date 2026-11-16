"""Tests for operational controls: freshness SLA, bounded retry and the alert log."""
from datetime import datetime, timedelta

import pytest

from ops_assistant.controls import AlertLog, stale_records, with_retry
from ops_assistant.tools import Evidence, TransientToolError

NOW = datetime(2026, 11, 15, 9, 0, 0)


def _ev(source_ts: str) -> Evidence:
    return Evidence("planned_production", "SCR-TOP-001", "2026-C12", 400, "units", "R1", source_ts)


def test_fresh_record_is_not_stale():
    assert stale_records([_ev((NOW - timedelta(hours=4)).isoformat())], NOW) == []


def test_record_older_than_sla_is_stale():
    old = _ev((NOW - timedelta(hours=25)).isoformat())
    assert stale_records([old], NOW) == [old]


def test_record_exactly_at_sla_is_not_stale():
    assert stale_records([_ev((NOW - timedelta(hours=24)).isoformat())], NOW) == []


def test_retry_recovers_after_transient_failure():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientToolError("blip")
        return "ok"

    alerts = AlertLog()
    assert with_retry(flaky, attempts=2, alerts=alerts) == "ok"
    assert alerts.codes() == ["TOOL_RETRY"]


def test_retry_gives_up_after_last_attempt():
    def always_fails():
        raise TransientToolError("down")

    alerts = AlertLog()
    with pytest.raises(TransientToolError):
        with_retry(always_fails, attempts=3, alerts=alerts)
    assert alerts.codes() == ["TOOL_RETRY"] * 3


def test_retry_does_not_swallow_other_errors():
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ValueError("not transient")

    with pytest.raises(ValueError):
        with_retry(broken, attempts=3)
    assert calls["n"] == 1
