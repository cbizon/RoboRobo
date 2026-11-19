"""The ``program`` command.

    program new DIR --name NAME --title TITLE --scientist WHO --data-dir PATH
    program sync      [--repo DIR]   re-render template-owned files
    program approve   [--repo DIR]   record the scientist's approval (human only)
    program check     [--repo DIR]   fail loudly if a rule didn't hold
    program map       [--repo DIR]   write the program map, an HTML page
    program run-night [--repo DIR] [--night YYYY-MM-DD] [--line L0001 ...] [--dry-run]
                                     run tonight's lines, one at a time
    program next-id E|C|D|B|L|T [--repo DIR]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from roborobo import config, mapview, night, render
from roborobo.approvals import approve
from roborobo.checks import check_program
from roborobo.config import CONFIG
from roborobo.gitutil import git
from roborobo.records import next_id

DEFAULT_SCHEDULE = {"session_start": "23:00", "session_end": "05:00",
                    "checker_minutes": "20", "debrief": "07:00"}


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
    removed = render.remove_retired(program)
    for path in changed:
        print(f"updated {path}")
    for path in created:
        print(f"created {path} (fill it in)")
    for path in removed:
        print(f"removed {path} (RoboRobo no longer uses it)")
    if not changed and not created and not removed:
        print("template-owned files already match")
    return 0


def cmd_approve(args) -> int:
    program = config.load(args.repo)
    tag = approve(program, args.remote)
    print(f"approved and pushed {tag}")
    return 0


def cmd_check(args) -> int:
    problems, notes = check_program(config.load(args.repo), args.remote)
    for note in notes:
        print(note)
    if problems:
        print(f"\nprogram check FAILED ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("program check passed")
    return 0


def cmd_map(args) -> int:
    print(mapview.write(config.load(args.repo), args.out, args.nights))
    return 0


def cmd_run_night(args) -> int:
    program = config.load(args.repo)
    when = args.night or night.night_of(program, datetime.now())
    code, text = night.run_night(program, when, args.line, args.dry_run)
    print(text, end="")
    return code


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
    pmap = sub.add_parser("map", help="write the program map, an HTML page built from the records")
    pmap.add_argument("--repo", type=Path, default=Path.cwd())
    pmap.add_argument("--out", type=Path, default=None,
                      help="where to write it (default: <data_dir>/map/index.html)")
    pmap.add_argument("--nights", type=int, default=14,
                      help="how many of the latest nights the map shows as columns")
    pmap.set_defaults(func=cmd_map)
    rn = sub.add_parser("run-night", help="run tonight's lines one at a time, each "
                        "followed by the checker")
    rn.add_argument("--repo", type=Path, default=Path.cwd())
    rn.add_argument("--night", type=date.fromisoformat, default=None,
                    help="the evening the night starts (default: tonight, or last night "
                         "before the session's end)")
    rn.add_argument("--line", action="append", default=None,
                    help="run only this planned line (repeatable)")
    rn.add_argument("--dry-run", action="store_true",
                    help="say which lines would run, and why, without running them")
    rn.set_defaults(func=cmd_run_night)
    nid = sub.add_parser("next-id", help="next free ledger id")
    nid.add_argument("letter", choices=["E", "C", "D", "B", "L", "T"])
    nid.add_argument("--repo", type=Path, default=Path.cwd())
    nid.set_defaults(func=cmd_next_id)
    args = ap.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
