"""The ``program`` command.

    program new DIR --name NAME --title TITLE --scientist WHO --data-dir PATH
    program sync      [--repo DIR]   re-render template-owned files
    program approve   [--repo DIR]   record the scientist's approval (human only)
    program check     [--repo DIR]   fail loudly if a rule didn't hold
    program next-id E|C|D|B [--repo DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from roborobo import config, render
from roborobo.approvals import approve, verify
from roborobo.config import CONFIG
from roborobo.gitutil import git
from roborobo.records import (blockers_carried, budgets, documents, ledger_consistency,
                              load_ledger, next_id)

DEFAULT_SCHEDULE = {"session_start": "23:00", "session_end": "05:00",
                    "checker": "05:30", "checker_minutes": "45", "debrief": "07:00"}


def cmd_new(args) -> int:
    repo = args.dir.expanduser().resolve()
    if repo.exists() and any(repo.iterdir()):
        raise SystemExit(f"{repo} is not empty; for an existing repository write "
                         f"{CONFIG} by hand and run `program sync`")
    repo.mkdir(parents=True, exist_ok=True)
    (repo / CONFIG).write_text(config.render_config(
        args.name, args.title, args.scientist, args.data_dir, DEFAULT_SCHEDULE,
        args.timezone))
    program = config.load(repo)
    render.write_template_owned(program)
    render.write_missing_seeded(program)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"Start program {args.name} from RoboRobo's template")
    print(f"Created {repo}. Next: write DIRECTION.md, BRIEF.md, budgets.yaml and the "
          "referee, add a remote, then run `program approve`.")
    return 0


def cmd_sync(args) -> int:
    program = config.load(args.repo)
    changed = render.write_template_owned(program)
    created = render.write_missing_seeded(program)
    for path in changed:
        print(f"updated {path}")
    for path in created:
        print(f"created {path} (fill it in)")
    if not changed and not created:
        print("template-owned files already match")
    return 0


def cmd_approve(args) -> int:
    program = config.load(args.repo)
    tag = approve(program, args.remote)
    print(f"approved and pushed {tag}")
    return 0


def cmd_check(args) -> int:
    program = config.load(args.repo)
    problems: list[str] = []
    notes: list[str] = []

    problems += [f"{path} differs from RoboRobo's template (run `program sync`)"
                 for path in render.template_drift(program)]
    human, facts = verify(program, args.remote)
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

    for note in notes:
        print(note)
    if problems:
        print(f"\nprogram check FAILED ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("program check passed")
    return 0


def cmd_next_id(args) -> int:
    print(next_id(config.load(args.repo), args.letter))
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="program", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create a program repository from the template")
    new.add_argument("dir", type=Path)
    new.add_argument("--name", required=True)
    new.add_argument("--title", required=True)
    new.add_argument("--scientist", required=True,
                     help="who steers the program, as the agent should call them")
    new.add_argument("--data-dir", required=True,
                     help="where large outputs go, outside git")
    new.add_argument("--timezone", default="America/New_York")
    new.set_defaults(func=cmd_new)
    for name, func, help_text in (
            ("sync", cmd_sync, "re-render template-owned files"),
            ("approve", cmd_approve, "record approval of human-only files (human only)"),
            ("check", cmd_check, "check that the program's rules held")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--repo", type=Path, default=Path.cwd())
        if name in ("approve", "check"):
            p.add_argument("--remote", default="origin")
        p.set_defaults(func=func)
    nid = sub.add_parser("next-id", help="next free ledger id")
    nid.add_argument("letter", choices=["E", "C", "D", "B"])
    nid.add_argument("--repo", type=Path, default=Path.cwd())
    nid.set_defaults(func=cmd_next_id)
    args = ap.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
