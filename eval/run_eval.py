"""Run the evaluation suite and print the release-gate scorecard.

    python eval/run_eval.py                 # print scorecard, exit 1 if any gate fails
    python eval/run_eval.py --report eval/RESULTS_latest.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from failure_injection import NOW, run_accuracy_scenarios, run_reliability_scenarios  # noqa: E402
from zaar_assistant.answer import answer_question  # noqa: E402
from zaar_assistant.db import build_database  # noqa: E402
from zaar_assistant.generator import fmt  # noqa: E402

GATES = [
    ("Numerical & citation accuracy", "claim-to-record match (metric+SKU+cycle+unit+record ID); tampered answers blocked"),
    ("Scope / partial-answer correctness", "labeled in-scope / mixed / out-of-scope / clarify questions -> expected response class"),
    ("Completeness & operational validity", "required qualifiers present; production in whole batches within capacity"),
    ("Reliability & regression", "timeouts, stale data, unauthorised role, validator crash, changed orders, V4 replay"),
]
THRESHOLD = 1.0  # synthetic suite: every check must pass before "release"


def evaluate_questions(conn) -> tuple[list[dict], list[tuple[bool, str]], list[tuple[bool, str]], list[tuple[bool, str]]]:
    questions = yaml.safe_load((HERE / "questions.yaml").read_text())
    rows, g1, g2, g3 = [], [], [], []
    for q in questions:
        r = answer_question(q["question"], q["role"], conn, now=NOW)
        exp = q["expect"]
        g2.append((r.status == exp["status"], f"{q['id']}: expected {exp['status']}, got {r.status}"))

        for v in exp.get("values", []):
            ev = next((e for e in r.evidence if e.metric == v["metric"] and e.entity == v["entity"]), None)
            ok = (ev is not None and ev.value == v["value"]
                  and ev.record_id in r.text and fmt(ev.value) in r.text)
            g1.append((ok, f"{q['id']}: {v['metric']} {v['entity']} == {v['value']} with citation"))

        for needle in exp.get("contains", []):
            g3.append((needle.lower() in r.text.lower(), f"{q['id']}: contains '{needle}'"))

        if r.status in ("answer", "partial"):  # operational validity on every plan we show
            for e in r.evidence:
                if e.metric == "planned_production":
                    bs = e.qualifiers["batch_size"]
                    g3.append((e.value % bs == 0, f"{q['id']}: {e.entity} planned {fmt(e.value)} is whole batches of {bs}"))
        rows.append({"id": q["id"], "status": r.status, "expected": exp["status"]})
    return rows, g1, g2, g3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", help="write the scorecard as markdown to this path")
    args = ap.parse_args()

    conn = build_database()
    rows, g1, g2, g3 = evaluate_questions(conn)
    acc, rel = run_accuracy_scenarios(), run_reliability_scenarios()
    g1 += [(s.passed, f"inject: {s.name}") for s in acc]
    g4 = [(s.passed, f"inject: {s.name}") for s in rel]

    results = [g1, g2, g3, g4]
    lines = ["| Gate | Passed | Score | Status |", "|---|---|---|---|"]
    failed = []
    for (name, _), checks in zip(GATES, results):
        p = sum(ok for ok, _ in checks)
        score = p / len(checks)
        status = "PASS" if score >= THRESHOLD else "FAIL"
        lines.append(f"| {name} | {p}/{len(checks)} | {score:.0%} | {status} |")
        failed += [msg for ok, msg in checks if not ok]

    n_q = len(rows)
    header = f"Evaluated {n_q} labeled questions + {len(acc)} accuracy and {len(rel)} reliability injections (synthetic data)\n"
    table = "\n".join(lines)
    print(header + "\n" + table)
    if failed:
        print("\nFAILED CHECKS:\n  " + "\n  ".join(failed))
    if args.report:
        Path(args.report).write_text(f"# Latest scorecard\n\n{header}\n{table}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
