from dataclasses import replace

from zaar_assistant.db import build_database
from zaar_assistant.generator import GeneratedAnswer, TemplateGenerator
from zaar_assistant.tools import run_tool
from zaar_assistant.validator import validate


def _evidence(sku="SCR-TOP-001"):
    return run_tool(build_database(), "get_production_plan", {"sku": sku, "cycle": "2026-C12"}, "production_manager")


def test_correct_answer_passes():
    ev = _evidence()
    assert validate(TemplateGenerator().generate("q", ev), ev).ok


def test_number_present_but_attached_to_wrong_entity_is_blocked():
    """The number 400 exists in the results, but not for this SKU/period: must still be blocked."""
    ev = _evidence()
    good = TemplateGenerator().generate("q", ev)
    bad = GeneratedAnswer(
        good.text.replace("SCR-TOP-001", "SCR-PNT-001"),
        [replace(c, entity="SCR-PNT-001") for c in good.claims],
    )
    res = validate(bad, ev)
    assert not res.ok and any("unsupported claim" in e for e in res.errors)


def test_value_mismatch_blocked():
    ev = _evidence()
    good = TemplateGenerator().generate("q", ev)
    claims = [replace(c, value=370) if c.metric == "planned_production" else c for c in good.claims]
    assert not validate(GeneratedAnswer(good.text, claims), ev).ok


def test_omitted_capacity_qualifier_blocked():
    ev = _evidence("SCR-PNT-001")
    good = TemplateGenerator().generate("q", ev)
    assert "Capacity exception" in good.text
    stripped = good.text.split(" Capacity exception")[0] + " [" + good.text.split("[")[-1]
    res = validate(GeneratedAnswer(stripped, good.claims), ev)
    assert not res.ok and any("capacity exception" in e for e in res.errors)


def test_unbacked_number_in_prose_blocked():
    ev = _evidence()
    good = TemplateGenerator().generate("q", ev)
    assert not validate(GeneratedAnswer(good.text + " Sales will rise 12%.", good.claims), ev).ok
