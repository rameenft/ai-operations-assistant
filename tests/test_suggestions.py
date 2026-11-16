"""Default questions, safety-stock answers and the manager-requested answer format."""
from datetime import datetime

import pytest

from ops_assistant.answer import ANSWER, REFUSE, answer_question
from ops_assistant.db import build_database
from ops_assistant.suggestions import DEFAULT_QUESTIONS

NOW = datetime(2026, 11, 15, 9, 0, 0)
PM, SM = "production_manager", "supply_manager"


@pytest.fixture()
def conn():
    return build_database()


@pytest.mark.parametrize("role,q", [(r, q) for r, qs in DEFAULT_QUESTIONS.items() for q in qs])
def test_every_default_question_gets_a_validated_answer(conn, role, q):
    r = answer_question(q, role, conn, now=NOW)
    assert r.status == ANSWER, r.text
    assert len(r.text.splitlines()) <= 14  # managers asked for short answers


def test_safety_stock_for_skus(conn):
    r = answer_question("What is the safety stock for scrub tops?", PM, conn, now=NOW)
    assert r.status == ANSWER
    assert [(e.metric, e.value) for e in r.evidence] == [("safety_stock", 100)]
    assert "FC-0001" in r.text and "100 units" in r.text


def test_safety_stock_for_materials_and_all(conn):
    r = answer_question("What are the safety stock levels for all materials?", SM, conn, now=NOW)
    assert {e.entity: e.value for e in r.evidence} == {"FAB-POLY-BLU": 200, "FAB-COT-WHT": 100, "BTN-STD": 300}
    r = answer_question("safety stock levels for all SKUs", PM, conn, now=NOW)
    assert {e.entity: e.value for e in r.evidence} == {"SCR-TOP-001": 100, "SCR-PNT-001": 150, "LAB-COT-001": 40}


def test_safety_stock_respects_roles(conn):
    assert answer_question("safety stock for BTN-STD", PM, conn, now=NOW).status == REFUSE
    assert answer_question("safety stock for scrub tops", SM, conn, now=NOW).status == REFUSE


def test_answer_format_leads_with_the_number_then_short_bullets(conn):
    r = answer_question("How many scrub tops should we produce?", PM, conn, now=NOW)
    lines = r.text.splitlines()
    assert lines[0].startswith("**SCR-TOP-001 (2026-C12): produce 400 units**")
    assert lines[1] == "- Net requirement: 370 units"
    assert r.text.rstrip().splitlines()[-1].startswith("_Source data as of")
    assert len(lines) <= 5


def test_list_answers_use_one_line_per_entity(conn):
    r = answer_question("Show the production plan for all SKUs", PM, conn, now=NOW)
    bullets = [ln for ln in r.text.splitlines() if ln.startswith("- **")]
    assert len(bullets) == 3
