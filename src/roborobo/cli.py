"""The ``program`` command.

    program new DIR --name NAME --title TITLE --scientist WHO --data-dir PATH
    program sync      [--repo DIR]   re-render template-owned files
    program approve   [--repo DIR]   record the scientist's approval (human only)
    program check     [--repo DIR]   fail loudly if a rule didn't hold
    program map       [--repo DIR]   write the program map, an HTML page
    program run-night [--repo DIR] [--night YYYY-MM-DD] [--line L0001 ...] [--dry-run]
                                     run tonight's lines, one at a time
    program next-id E|C|D|B|L|T|A [--repo DIR]
    program submit --host H --experiment E [--rev REV] [--tree NAME=REV ...]
                   [--input PATH ...] [--time T] [--mem M] [--cpus N] [--repo DIR]
                   -- COMMAND...                run a job on a cluster
    program jobs      [--repo DIR]   check jobs in flight; record the ones that ended
    program record PATH [--experiment E] [--line L] [--input REF ...] [--note TEXT]
                                     record a data folder here (or its new manifest)
    program delete A0123 --reason TEXT   delete a data folder, here or on a cluster
    program fetch A0123[:PATH] [--max-mb N]   copy cluster data into tmp/fetch/ to look at
    program serve --repo DIR [--repo DIR ...] [--state DIR] [--poll-minutes N] [--once]
                                     the always-on service: run lines when there is work
    program serve --resume NAME [--state DIR]   let a paused program run again
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

from roborobo import artifacts, cluster, config, mapview, night, render, service, service_api
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


def cmd_submit(args) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    job = cluster.submit(config.load(args.repo), args.host, args.experiment, command,
                         args.rev, args.time, args.mem, args.cpus, args.tree or [],
                         args.input or [])
    print(f"submitted {job['experiment_id']} as job {job['job_id']} on {job['host']} "
          f"(commit {job['commit'][:10]}, {job['run_dir']})")
    return 0


def cmd_jobs(args) -> int:
    report = cluster.refresh(config.load(args.repo))
    print(cluster.describe(report), end="")
    return 1 if report.problems else 0


def cmd_record(args) -> int:
    program = config.load(args.repo)
    if args.backfill:
        for path in artifacts.unrecorded(program):
            aid, _ = artifacts.record_local(program, path.rstrip("/"), by=args.by,
                                            provenance="backfilled",
                                            note="recorded after the fact; provenance unknown")
            print(f"{aid} {path}")
        return 0
    if not args.path:
        raise SystemExit("give a folder to record, or --backfill")
    aid, what = artifacts.record_local(program, args.path, args.experiment, args.line,
                                       args.input or [], args.note, args.by)
    print(f"{what} {aid} for {artifacts.local_rel(program, args.path)}; commit the ledger")
    return 0


def cmd_delete(args) -> int:
    where = artifacts.delete(config.load(args.repo), args.artifact, args.reason, args.by)
    print(f"deleted {args.artifact} ({where}); commit the ledger")
    return 0


def cmd_fetch(args) -> int:
    print(artifacts.fetch(config.load(args.repo), args.ref, args.max_mb))
    return 0


def cmd_serve(args) -> int:
    state = args.state.expanduser()
    if args.resume:
        conn = service.connect(state / "service.db")
        if not service.resume(conn, args.resume):
            raise SystemExit(f"{args.resume} isn't paused")
        print(f"{args.resume} will run again at the next look")
        return 0
    if not args.repo:
        raise SystemExit("give at least one --repo")
    service.serve([r.expanduser() for r in args.repo], state, args.poll_minutes, args.once,
                  args.dashboard_host, args.dashboard_port, args.api_port)
    return 0


def main(argv=None) -> None:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if (os.environ.get("ROBOROBO_SERVICE_URL") and args_in
            and args_in[0] in service_api.FORWARDED):
        # An agent pod holds no keys: the service runs this command for it.
        sys.exit(service_api.forward(args_in))
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
    nid.add_argument("letter", choices=["E", "C", "D", "B", "L", "T", "A"])
    nid.add_argument("--repo", type=Path, default=Path.cwd())
    nid.set_defaults(func=cmd_next_id)
    sj = sub.add_parser("submit", help="run a job on a cluster in program.toml [hosts]")
    sj.add_argument("--repo", type=Path, default=Path.cwd())
    sj.add_argument("--host", required=True)
    sj.add_argument("--experiment", required=True, help="the registered experiment it serves")
    sj.add_argument("--rev", default="HEAD", help="the commit whose code runs (default HEAD)")
    sj.add_argument("--tree", action="append", metavar="NAME=REV",
                    help="also check out REV of this repository at ../NAME, e.g. "
                         "engine=method/nudge, or NAME=PATH:REV for another git "
                         "repository in the data folder (repeatable)")
    sj.add_argument("--input", action="append", metavar="REF",
                    help="a recorded artifact, or part of one, the job starts from: "
                         "A0123[:path] or a path in the data folder; it lands in "
                         "$ROBOROBO_IN (repeatable)")
    sj.add_argument("--time", help="Slurm time limit, e.g. 6:00:00 (default: the host's)")
    sj.add_argument("--mem", help="memory, e.g. 16G (default: the host's)")
    sj.add_argument("--cpus", type=int, help="CPUs per task (default: the host's)")
    sj.add_argument("command", nargs=argparse.REMAINDER,
                    help="after --: the command, run from the code's top folder")
    sj.set_defaults(func=cmd_submit)
    jb = sub.add_parser("jobs", help="check jobs in flight, sync their outputs into "
                        "<data_dir>/runs/, and record the ones that ended")
    jb.add_argument("--repo", type=Path, default=Path.cwd())
    jb.set_defaults(func=cmd_jobs)
    actor = {"choices": ["agent", "human"], "default": "agent",
             "help": "who is doing this (default agent)"}
    rc = sub.add_parser("record", help="record a data folder here as an artifact, or a new "
                        "manifest for one already recorded")
    rc.add_argument("path", nargs="?", help="a folder in the data folder, e.g. runs/E0101")
    rc.add_argument("--repo", type=Path, default=Path.cwd())
    rc.add_argument("--experiment", help="default: from a runs/E0123 path")
    rc.add_argument("--line", help="default: the experiment's line")
    rc.add_argument("--input", action="append", metavar="REF",
                    help="an artifact it was made from: A0123[:path] (repeatable)")
    rc.add_argument("--note")
    rc.add_argument("--by", **actor)
    rc.add_argument("--backfill", action="store_true",
                    help="record everything unrecorded in the data folder, provenance unknown")
    rc.set_defaults(func=cmd_record)
    dl = sub.add_parser("delete", help="delete a data folder, here or on a cluster, and "
                        "record it")
    dl.add_argument("artifact")
    dl.add_argument("--reason", required=True)
    dl.add_argument("--repo", type=Path, default=Path.cwd())
    dl.add_argument("--by", **actor)
    dl.set_defaults(func=cmd_delete)
    ft = sub.add_parser("fetch", help="copy a cluster artifact, or part of it, into "
                        "tmp/fetch/ to look at; emptied after each run")
    ft.add_argument("ref", metavar="A0123[:PATH]")
    ft.add_argument("--repo", type=Path, default=Path.cwd())
    ft.add_argument("--max-mb", type=int, default=None,
                    help="refuse anything bigger (default: the host's fetch_max_mb)")
    ft.set_defaults(func=cmd_fetch)
    sv = sub.add_parser("serve", help="the always-on service: look at each program every few "
                        "minutes and run lines that have new events")
    sv.add_argument("--repo", type=Path, action="append",
                    help="a program repository to serve (repeatable)")
    sv.add_argument("--state", type=Path, default=Path("~/.roborobo"),
                    help="where the service keeps its database and status.md")
    sv.add_argument("--poll-minutes", type=float, default=5)
    sv.add_argument("--once", action="store_true", help="look once and stop")
    sv.add_argument("--resume", metavar="NAME", help="let a paused program run again")
    sv.add_argument("--dashboard-host", default="127.0.0.1",
                    help="where the dashboard listens (0.0.0.0 in a pod)")
    sv.add_argument("--dashboard-port", type=int, default=8080,
                    help="the dashboard's port; 0 for no dashboard")
    sv.add_argument("--api-port", type=int, default=0,
                    help="where agent pods send submit/jobs/fetch (0: no agent pods)")
    sv.set_defaults(func=cmd_serve)
    args = ap.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
