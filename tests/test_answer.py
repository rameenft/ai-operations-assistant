"""Tests for the answer pipeline: it must return validated text or fail closed."""
from datetime import datetime, timedelta

from ops_assistant.answer import ANSWER, REFUSE, SAFE_FAILURE, answer_question
from ops_assistant.db import build_database

PM = "production_manager"
SM = "supply_manager"
NOW = datetime(2026, 11, 15, 9, 0, 0)  # a few hours after the synthetic source timestamps
Q = "What is the planned production for SCR-TOP-001 in 2026-C12?"


def test_happy_path_returns_answer_with_as_of_footer():
    r = answer_question(Q, PM, build_database(), now=NOW)
    assert r.status == ANSWER
    assert "Source data as of" in r.text


def test_stale_data_blocks_answer():
    r = answer_question(Q, PM, build_database(), now=NOW + timedelta(days=3))
    assert r.status == SAFE_FAILURE and r.reason == "stale_data"
    assert "STALE_INPUTS" in r.alerts.codes()


def test_wrong_role_is_refused():
    r = answer_question(Q, SM, build_database(), now=NOW)
    assert r.status == REFUSE and r.reason == "unauthorized"


def test_unexpected_exception_fails_closed():
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    r = answer_question(Q, PM, build_database(), tool_runner=boom, now=NOW)
    assert r.status == SAFE_FAILURE and r.reason == "exception"
    assert "PIPELINE_EXCEPTION" in r.alerts.codes()
