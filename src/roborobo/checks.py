"""Everything `program check` checks, as one function the night runner can
call between lines."""

from __future__ import annotations

from roborobo import render
from roborobo.approvals import verify
from roborobo.config import Program
from roborobo.records import (blockers_carried, budgets, documents, ledger_consistency,
                              load_ledger)


def check_program(program: Program, remote: str = "origin") -> tuple[list[str], list[str]]:
    """Problems (any one fails the check) and notes to show alongside."""
    problems: list[str] = []
    notes: list[str] = []

    problems += [f"{path} differs from RoboRobo's template (run `program sync`)"
                 for path in render.template_drift(program)]
    human, facts = verify(program, remote)
    problems += human
    if facts:
        notes.append(f"latest approval: {facts['latest_approval']}")
        if facts["approvals_last_day"]:
            notes.append("approvals in the last day (check these were yours): "
                         + ", ".join(facts["approvals_last_day"]))

    records, ledger_problems = load_ledger(program)
    problems += ledger_problems + ledger_consistency(records)
    summaries, summary_problems = documents(program, "sessions", "session-summary.schema.json")
    _, review_problems = documents(program, "reviews", "review.schema.json")
    problems += summary_problems + review_problems + blockers_carried(records, summaries)
    budget_problems, budget_report = budgets(program, records)
    problems += budget_problems
    notes += [f"budget {line}" for line in budget_report]
    return problems, notes
