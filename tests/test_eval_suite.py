"""The evaluation suite is the release gate: if any gate regresses, CI (pytest) fails."""
import run_eval
from failure_injection import run_accuracy_scenarios, run_reliability_scenarios


def test_accuracy_injections_all_blocked():
    failed = [r for r in run_accuracy_scenarios() if not r.passed]
    assert not failed, failed


def test_reliability_scenarios_all_pass():
    failed = [r for r in run_reliability_scenarios() if not r.passed]
    assert not failed, failed


def test_full_scorecard_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["run_eval.py"])
    assert run_eval.main() == 0
    assert "FAIL" not in capsys.readouterr().out
