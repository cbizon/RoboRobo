"""The always-on service: ``program serve``.

Instead of one night at 11 pm, the service looks at each program every few
minutes and runs a line when there is something for it to do:

- **instruction**: the newest instruction file has a section for the line
  that the line hasn't run on yet, or that changed since (the section and
  the text above the first section count);
- **challenge**: the line's latest review has a claim judged doubtful or
  broken;
- **job**: one of the line's cluster jobs ended (``program jobs`` records
  it, as a line would).

Each event is acted on once. A run is the same as a night's: the worker,
then the checker on what it produced, one line at a time per program, so
records still commit on ``main``. Limits in ``program.toml`` (``[service]``)
cap runs per line per day and set the least gap between them.

Before looking for events the service brings its clone up to date with the
remote (merging what the scientist pushed: instructions, approvals) and
pushes what runs committed. Nothing is retried quietly: a run that leaves
the repository dirty or ``program check`` failing, or a merge that
conflicts, pauses the program until ``program serve --resume <name>``.
Errors reaching a host or the remote are shown in the status file and
tried again at the next look. The first look at a program records the
events already there without running anything, so starting the service
doesn't rerun what the night runner did.

State is a small SQLite database (events, runs, paused programs), kept
outside the program repositories; ``status.md`` beside it says what the
service is doing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from roborobo import cluster, config, dashboard
from roborobo.config import Program
from roborobo.gitutil import git
from roborobo.mapview import fold_lines
from roborobo.night import LINE_HEADING, challenged_lines, run_line, uncommitted
from roborobo.records import load_ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    program TEXT NOT NULL,
    line TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL,
    status TEXT NOT NULL,        -- pending, done, seen (recorded at the first look)
    run INTEGER,
    UNIQUE (program, key)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    program TEXT NOT NULL,
    line TEXT NOT NULL,
    day TEXT NOT NULL,
    stem TEXT NOT NULL,
    started TEXT NOT NULL,
    ended TEXT,
    reasons TEXT NOT NULL,
    summary TEXT,
    review TEXT,
    problems TEXT
);
CREATE TABLE IF NOT EXISTS paused (
    program TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now_utc() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def today(program: Program, at: datetime) -> date:
    return at.astimezone(ZoneInfo(program.timezone)).date()


# --- keeping the clone in step with the remote ---------------------------------

def sync(program: Program, remote: str = "origin") -> str | None:
    """Merge what was pushed and push what runs committed. Returns a reason
    to pause the program, or None. Network errors raise, to be retried."""
    repo = program.repo
    dirty = uncommitted(program)
    if dirty:
        return "the repository has uncommitted changes: " + "; ".join(dirty[:5])
    git(repo, "fetch", "-q", remote)
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    upstream = f"{remote}/{branch}"
    behind = int(git(repo, "rev-list", "--count", f"HEAD..{upstream}").strip())
    ahead = int(git(repo, "rev-list", "--count", f"{upstream}..HEAD").strip())
    if behind:
        merged = git(repo, "merge", "-q", "--no-edit", upstream, check=False)
        if uncommitted(program) or git(repo, "rev-list", "--count",
                                       f"HEAD..{upstream}").strip() != "0":
            git(repo, "merge", "--abort", check=False)
            return f"merging {upstream} conflicts; resolve it in the service's clone ({merged.strip()})"
    if ahead or behind:
        git(repo, "push", "-q", remote, f"HEAD:{branch}")
    return None


# --- events ---------------------------------------------------------------------

def add_event(conn, program: Program, line: str, kind: str, key: str, detail: str,
              status: str = "pending") -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO events (program, line, kind, key, detail, at, status) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (program.name, line, kind, key, detail, now_utc().isoformat(), status))
    return cur.rowcount == 1


def newest_instruction(program: Program, day: date) -> Path | None:
    files = sorted(p for p in (program.repo / "instructions").glob("20*.md")
                   if p.stem <= day.isoformat())
    return files[-1] if files else None


def instruction_events(program: Program, day: date) -> list[tuple[str, str, str]]:
    """(line, key, detail) for each line section of the newest instruction."""
    path = newest_instruction(program, day)
    if path is None:
        return []
    text = path.read_text()
    marks = list(LINE_HEADING.finditer(text))
    preamble = text[:marks[0].start()] if marks else text
    rel = path.relative_to(program.repo).as_posix()
    found = []
    for i, mark in enumerate(marks):
        section = text[mark.start():marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        digest = hashlib.sha256((preamble + section).encode()).hexdigest()[:12]
        found.append((mark[1], f"instruction:{rel}:{mark[1]}:{digest}", rel))
    return found


def job_events(program: Program) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Record ended jobs as a line would (program jobs) and commit what that
    wrote; one event per ended job with a line."""
    if not program.hosts:
        return [], []
    report = cluster.refresh(program)
    found = []
    for job in report.ended:
        if job.get("line_id"):
            detail = (f"job {job['host']}-{job['job_id']} ({job['experiment_id']}) ended "
                      f"{job['state']}, artifact {job.get('artifact_id')}")
            found.append((job["line_id"], f"job:{job['host']}-{job['job_id']}", detail))
    if uncommitted(program):
        git(program.repo, "add", "ledger", "evidence")
        ids = ", ".join(f"{j['host']}-{j['job_id']}" for j in report.ended) or "mirror"
        git(program.repo, "commit", "-q", "-m", f"Record ended jobs: {ids}")
    return found, report.problems


def collect(conn, program: Program, day: date) -> list[str]:
    """Look for new events; on the first look at a program, record them as
    seen without running anything. Returns notes for the status file."""
    first = conn.execute("SELECT COUNT(*) FROM events WHERE program = ?",
                         (program.name,)).fetchone()[0] == 0
    status = "seen" if first else "pending"
    notes = []
    for line, key, detail in instruction_events(program, day):
        add_event(conn, program, line, "instruction", key, detail, status)
    for line, review in challenged_lines(program).items():
        add_event(conn, program, line, "challenge", f"challenge:{review}",
                  f"open challenges in {review}", status)
    jobs, problems = job_events(program)
    for line, key, detail in jobs:
        add_event(conn, program, line, "job", key, detail, status)
    notes += [f"job problem: {p}" for p in problems]
    if first:
        # Mark something so a program with no events yet isn't "first" forever.
        add_event(conn, program, "-", "start", f"start:{now_utc().isoformat()}",
                  "service started", "seen")
    conn.commit()
    return notes


# --- runs ----------------------------------------------------------------------

def active_lines(program: Program) -> set[str]:
    records, problems = load_ledger(program)
    if problems:
        raise ValueError("the ledger doesn't validate: " + "; ".join(problems[:3]))
    return {lid for lid, line in fold_lines(records["lines.jsonl"]).items()
            if line.get("status") == "active"}


@dataclass
class Due:
    line: str
    events: list[sqlite3.Row]


def due(conn, program: Program, at: datetime) -> tuple[list[Due], list[str]]:
    """Lines with pending events that may run now, oldest event first, and
    notes on the ones that must wait."""
    active = active_lines(program)
    limits = program.service
    day = today(program, at).isoformat()
    pending = conn.execute("SELECT * FROM events WHERE program = ? AND status = 'pending' "
                           "ORDER BY id", (program.name,)).fetchall()
    by_line: dict[str, list] = {}
    for event in pending:
        by_line.setdefault(event["line"], []).append(event)
    ready, waiting = [], []
    for line, events in by_line.items():
        if line not in active:
            conn.execute("UPDATE events SET status = 'dropped' WHERE program = ? AND line = ? "
                         "AND status = 'pending'", (program.name, line))
            waiting.append(f"{line} isn't active; dropped {len(events)} event(s)")
            continue
        runs_today = conn.execute("SELECT COUNT(*) FROM runs WHERE program = ? AND line = ? "
                                  "AND day = ?", (program.name, line, day)).fetchone()[0]
        if runs_today >= limits["max_runs_per_line_per_day"]:
            waiting.append(f"{line} has had {runs_today} runs today (the limit)")
            continue
        last = conn.execute("SELECT started FROM runs WHERE program = ? AND line = ? "
                            "ORDER BY id DESC LIMIT 1", (program.name, line)).fetchone()
        if last:
            gap = timedelta(minutes=limits["min_minutes_between_runs"])
            next_start = datetime.fromisoformat(last["started"]) + gap
            if at < next_start:
                waiting.append(f"{line} waits until {next_start:%H:%M} (minimum gap)")
                continue
        ready.append(Due(line, events))
    conn.commit()
    return ready, waiting


def prompts(program: Program, line: str, stem: str, events: list) -> tuple[str, str, list[str]]:
    reasons = [e["detail"] for e in events]
    instruction = next((e["detail"] for e in reversed(events) if e["kind"] == "instruction"),
                       None)
    worker = [f"Run line {line} of the {program.name} research program. Work in "
              f"{program.repo}: read scheduled/line.md and follow it. The always-on service "
              f"started this run, so write your summary to sessions/{stem}.json. What started "
              "it: " + "; ".join(reasons) + "."]
    if instruction:
        worker.append(f"Your part of the instruction is the `## {line}` section of "
                      f"{instruction}, with anything above the first line section. If an "
                      "earlier run already answered it, deal only with what is new.")
    else:
        worker.append("There is no new instruction for this line: deal with what started the "
                      "run, and if nothing needs doing, say so in the summary and stop.")
    followed = instruction or "none: started by the service for " + ", ".join(
        sorted({e["kind"] for e in events}))
    worker.append(f'Set "instruction" in the summary to "{followed}".')
    checker = (f"Check line {line}'s run of the {program.name} research program. Work in "
               f"{program.repo}: read scheduled/line-checker.md and follow it for "
               f"sessions/{stem}.json, and write your review to reviews/{stem}.json.")
    return " ".join(worker), checker, reasons


def run(conn, program: Program, item: Due, at: datetime) -> tuple[bool, str]:
    """Run one line for its pending events. Returns whether the program can go
    on, and a line for the status file."""
    day = today(program, at)
    count = conn.execute("SELECT COUNT(*) FROM runs WHERE program = ? AND line = ? AND day = ?",
                         (program.name, item.line, day.isoformat())).fetchone()[0]
    stem = f"{day.isoformat()}-{item.line}" + (f"-{count + 1}" if count else "")
    worker, checker, reasons = prompts(program, item.line, stem, item.events)
    cur = conn.execute("INSERT INTO runs (program, line, day, stem, started, reasons) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (program.name, item.line, day.isoformat(), stem, at.isoformat(),
                        json.dumps(reasons)))
    run_id = cur.lastrowid
    conn.executemany("UPDATE events SET status = 'done', run = ? WHERE id = ?",
                     [(run_id, e["id"]) for e in item.events])
    conn.commit()
    logs = Path(program.data_dir).expanduser() / "logs" / "service" / day.isoformat()
    result, go_on = run_line(program, day, item.line, reasons, None, logs, stem=stem,
                             worker_text=worker, checker_text=checker)
    conn.execute("UPDATE runs SET ended = ?, summary = ?, review = ?, problems = ? WHERE id = ?",
                 (now_utc().isoformat(), result.summary, result.review,
                  json.dumps(result.problems), run_id))
    conn.commit()
    note = (f"{stem}: {result.minutes:.0f} min, summary {result.summary or 'none'}, "
            f"review {result.review or 'none'}"
            + (f"; problems: {'; '.join(result.problems)}" if result.problems else ""))
    return go_on, note


def pause(conn, program: Program, reason: str) -> None:
    conn.execute("INSERT OR REPLACE INTO paused (program, at, reason) VALUES (?, ?, ?)",
                 (program.name, now_utc().isoformat(), reason))
    conn.commit()


def resume(conn, name: str) -> bool:
    cur = conn.execute("DELETE FROM paused WHERE program = ?", (name,))
    conn.commit()
    return cur.rowcount == 1


# --- one look at a program, and the loop ---------------------------------------

def look(conn, program: Program, at: datetime | None = None) -> list[str]:
    """Sync, collect events, and run every line that is due. Returns notes."""
    at = at or now_utc()
    paused = conn.execute("SELECT reason FROM paused WHERE program = ?",
                          (program.name,)).fetchone()
    if paused:
        return [f"paused: {paused['reason']} (resume with `program serve --resume "
                f"{program.name}`)"]
    reason = sync(program)
    if reason:
        pause(conn, program, reason)
        return [f"paused: {reason}"]
    try:
        notes = collect(conn, program, today(program, at))
    except ValueError as error:  # reviews or the ledger don't validate
        pause(conn, program, str(error))
        return [f"paused: {error}"]
    reason = sync(program)  # push the job records collect committed
    if reason:
        pause(conn, program, reason)
        return notes + [f"paused: {reason}"]
    try:
        ready, waiting = due(conn, program, at)
    except ValueError as error:  # the ledger doesn't validate
        pause(conn, program, str(error))
        return notes + [f"paused: {error}"]
    notes += [f"waiting: {w}" for w in waiting]
    for item in ready:
        go_on, note = run(conn, program, item, now_utc())
        notes.append(f"ran {note}")
        if not go_on:
            pause(conn, program, f"{item.line}'s run left the repository in a state the next "
                                 "run can't start from; see its logs")
            notes.append("paused after that run")
            break
        reason = sync(program)
        if reason:
            pause(conn, program, reason)
            notes.append(f"paused: {reason}")
            break
    return notes


def write_status(state: Path, conn, looks: dict[str, list[str]]) -> None:
    out = [f"# RoboRobo service", "", f"Last look: {now_utc().isoformat()}", ""]
    for name, notes in looks.items():
        out.append(f"## {name}")
        out += [f"- {n}" for n in notes] or ["- nothing to do"]
        pending = conn.execute("SELECT line, detail FROM events WHERE program = ? AND "
                               "status = 'pending' ORDER BY id", (name,)).fetchall()
        out += [f"- pending: {r['line']}: {r['detail']}" for r in pending]
        recent = conn.execute("SELECT stem, ended, problems FROM runs WHERE program = ? "
                              "ORDER BY id DESC LIMIT 5", (name,)).fetchall()
        out += [f"- recent run: {r['stem']} (ended {r['ended'] or 'running'})"
                + (f", problems {r['problems']}" if r["problems"] not in (None, "[]") else "")
                for r in recent]
        out.append("")
    (state / "status.md").write_text("\n".join(out))


def serve(repos: list[Path], state: Path, poll_minutes: float, once: bool = False,
          dashboard_host: str = "127.0.0.1", dashboard_port: int = 8080) -> None:
    state = state.expanduser()
    conn = connect(state / "service.db")
    if dashboard_port:
        dashboard.serve_http(state, dashboard_host, dashboard_port)
        print(f"dashboard on http://{dashboard_host}:{dashboard_port}/", flush=True)
    while True:
        looks, errors, programs = {}, {}, []
        for repo in repos:
            try:
                program = config.load(repo)
                programs.append(program)
                looks[program.name] = look(conn, program)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                # git, ssh or rsync failed (a host or the remote unreachable): shown in
                # the status and on the dashboard, and tried again at the next look.
                # Anything else is a bug and stops the service, so it is seen and
                # restarted.
                errors[Path(repo).name] = traceback.format_exc().strip().splitlines()[-6:]
        write_status(state, conn, {**looks, **{n: ["error (tried again at the next look):",
                                                    *lines] for n, lines in errors.items()}})
        dashboard.write(state, conn, programs, looks, errors)
        for name, notes in [*looks.items(), *errors.items()]:
            for note in notes:
                print(f"{now_utc():%Y-%m-%d %H:%M} {name}: {note}", flush=True)
        if once:
            return
        time.sleep(poll_minutes * 60)
