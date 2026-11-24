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
tried again at the next look. On the first look at a program, an event the
line has already run on (it has a summary committed after the event) is
recorded as seen, and anything else is pending: starting the service
neither reruns nor skips what the night runner did or didn't do.

State is a small SQLite database (events, runs, paused programs), kept
outside the program repositories; ``status.md`` beside it says what the
service is doing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from roborobo import ROOT, actions, cluster, config, dashboard, lines, render, service_api
from roborobo.config import Program
from roborobo.gitutil import committed_at, git
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
    status TEXT NOT NULL,        -- pending, done, seen (recorded at the first look),
                                 -- held (the scientist's instruction, until Start)
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
CREATE TABLE IF NOT EXISTS holds (       -- the program waits, e.g. for the Claude usage limit
    program TEXT PRIMARY KEY,
    until TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timers (      -- wake-ups a line set for itself (program wake)
    id INTEGER PRIMARY KEY,
    program TEXT NOT NULL,
    line TEXT NOT NULL,
    due TEXT NOT NULL,
    reason TEXT NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS paused (
    program TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paused_lines (
    program TEXT NOT NULL,
    line TEXT NOT NULL,
    at TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (program, line)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "counted" not in {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}:
        # 1: a run that used one of the line's runs for the day; 0: one that ended
        # without a summary (a setup failure, or the usage limit)
        conn.execute("ALTER TABLE runs ADD COLUMN counted INTEGER NOT NULL DEFAULT 1")
        conn.commit()
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


def sync_template(program: Program) -> list[str]:
    """Bring the files RoboRobo renders (the brief, prompts, Claude Code
    settings) in line with this RoboRobo, and commit them. They are RoboRobo's
    files, so the service keeps them current itself after RoboRobo changes;
    the scientist's files are never touched."""
    changed = [*render.write_template_owned(program), *render.write_missing_seeded(program),
               *render.remove_retired(program)]
    if changed:
        git(program.repo, "add", "-A", "--", *changed)
        git(program.repo, "commit", "-q", "-m", "Sync from RoboRobo: " + ", ".join(changed))
    return changed


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


def line_instruction_events(program: Program) -> list[tuple[str, str, str]]:
    """(line, key, detail) for each line's own instruction,
    lines/<line>/instruction.md, keyed by its contents."""
    found = []
    for path in sorted((program.repo / "lines").glob("L[0-9][0-9][0-9][0-9]/instruction.md")):
        rel = path.relative_to(program.repo).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        found.append((path.parent.name, f"instruction:{rel}:{digest}", rel))
    return found


def timer_events(conn, program: Program, at: datetime) -> list[tuple[str, str, str]]:
    """Timers lines set for themselves (program wake) that have come due."""
    found = []
    for row in conn.execute("SELECT * FROM timers WHERE program = ? AND fired = 0 ORDER BY id",
                            (program.name,)).fetchall():
        if datetime.fromisoformat(row["due"]) <= at:
            found.append((row["line"], f"timer:{row['id']}",
                          f"the timer this line set: {row['reason']}"))
            conn.execute("UPDATE timers SET fired = 1 WHERE id = ?", (row["id"],))
    return found


def start_line(conn, program: Program, line: str) -> int:
    """The scientist's Start: the line's held instructions are ready, and the
    line runs next whatever its limits. Returns how many instructions it
    releases."""
    released = conn.execute("UPDATE events SET status = 'pending' WHERE program = ? AND line = ? "
                            "AND status = 'held'", (program.name, line)).rowcount
    stamp = now_utc().isoformat()
    conn.execute("INSERT INTO events (program, line, kind, key, detail, at, status) "
                 "VALUES (?, ?, 'start', ?, ?, ?, 'pending')",
                 (program.name, line, f"start:{line}:{stamp}",
                  f"{program.scientist} pressed Start", stamp))
    conn.commit()
    return released


def set_timer(database: Path, program: Program, line: str, due: datetime, reason: str) -> None:
    """Record a wake-up for a line (program wake). At most three waiting per
    line, between ten minutes and seven days ahead."""
    ahead = due - now_utc()
    if not timedelta(minutes=10) <= ahead <= timedelta(days=7):
        raise SystemExit("a timer must be between 10 minutes and 7 days ahead")
    conn = connect(database)
    waiting = conn.execute("SELECT COUNT(*) FROM timers WHERE program = ? AND line = ? AND "
                           "fired = 0", (program.name, line)).fetchone()[0]
    if waiting >= 3:
        raise SystemExit(f"{line} already has {waiting} timers waiting; that is the most")
    conn.execute("INSERT INTO timers (program, line, due, reason) VALUES (?, ?, ?, ?)",
                 (program.name, line, due.isoformat(), reason))
    conn.commit()


def job_events(program: Program) -> tuple[list[tuple[str, str, str, int]], list[str]]:
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
            ended = int(datetime.fromisoformat(job["ended_at"].replace("Z", "+00:00"))
                        .timestamp())
            found.append((job["line_id"], f"job:{job['host']}-{job['job_id']}", detail, ended))
    if uncommitted(program):
        git(program.repo, "add", "ledger", "evidence")
        ids = ", ".join(f"{j['host']}-{j['job_id']}" for j in report.ended) or "mirror"
        git(program.repo, "commit", "-q", "-m", f"Record ended jobs: {ids}")
    return found, report.problems


def last_summary_committed(program: Program, line: str) -> int:
    """When the line's newest session summary was committed (0 if never)."""
    return max((committed_at(program.repo, p.relative_to(program.repo).as_posix())
                for p in (program.repo / "sessions").glob(f"*-{line}*.json")), default=0)


def collect(conn, program: Program, day: date) -> list[str]:
    """Look for new events. On the first look at a program, an event whose
    line already has a summary committed after it (the night runner, or an
    earlier service, ran on it) is recorded as seen; anything the line hasn't
    run on yet is pending, so starting the service neither redoes nor skips
    work. Returns notes for the status file."""
    first = conn.execute("SELECT COUNT(*) FROM events WHERE program = ?",
                         (program.name,)).fetchone()[0] == 0

    def status(line: str, since: int) -> str:
        if first and last_summary_committed(program, line) > since:
            return "seen"
        return "pending"

    notes = []
    for line, key, detail in instruction_events(program, day):
        add_event(conn, program, line, "instruction", key, detail,
                  status(line, committed_at(program.repo, detail)))
    # A line's own instruction is what the scientist is still writing (answers
    # from the dashboard, a discussion's conclusion): it waits, held, until the
    # scientist presses Start for the line.
    for line, key, detail in line_instruction_events(program):
        seen = status(line, committed_at(program.repo, detail)) == "seen"
        add_event(conn, program, line, "instruction", key, detail, "seen" if seen else "held")
    for line, key, detail in timer_events(conn, program, now_utc()):
        add_event(conn, program, line, "timer", key, detail)
    for line, review in challenged_lines(program).items():
        add_event(conn, program, line, "challenge", f"challenge:{review}",
                  f"open challenges in {review}", status(line, committed_at(program.repo, review)))
    # Recording an ended job takes a ledger id from this same database on a
    # connection of its own (records.reserve), so nothing may be left uncommitted
    # here, or the two wait on each other until "database is locked".
    conn.commit()
    jobs, problems = job_events(program)
    for line, key, detail, ended in jobs:
        add_event(conn, program, line, "job", key, detail, status(line, ended))
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
    hold = conn.execute("SELECT until, reason FROM holds WHERE program = ?",
                        (program.name,)).fetchone()
    if hold:
        until = datetime.fromisoformat(hold["until"])
        if at < until:
            return [], [f"{hold['reason']}; runs start again at {clock(program, until)}"]
        conn.execute("DELETE FROM holds WHERE program = ?", (program.name,))
    active = active_lines(program)
    held = {r["line"]: r["reason"] for r in conn.execute(
        "SELECT line, reason FROM paused_lines WHERE program = ?", (program.name,))}
    limits = program.service
    day = today(program, at).isoformat()
    pending = conn.execute("SELECT * FROM events WHERE program = ? AND status = 'pending' "
                           "ORDER BY id", (program.name,)).fetchall()
    unstarted = {r["line"] for r in conn.execute(
        "SELECT DISTINCT line FROM events WHERE program = ? AND status = 'held'",
        (program.name,))}
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
        if line in held:
            waiting.append(f"{line} is paused: {held[line]} (resume with `program serve "
                           f"--resume {program.name}:{line}`)")
            continue
        asked = any(e["kind"] == "start" for e in events)
        if line in unstarted and not asked:
            waiting.append(f"{line} has instructions from {program.scientist} that aren't "
                           "started; its runs wait for Start")
            continue
        if asked:  # the scientist asked for this run: no daily limit, no gap
            ready.append(Due(line, events))
            continue
        runs_today, failed_today = conn.execute(
            "SELECT COALESCE(SUM(counted), 0), COALESCE(SUM(1 - counted), 0) FROM runs "
            "WHERE program = ? AND line = ? AND day = ?", (program.name, line, day)).fetchone()
        if runs_today >= limits["max_runs_per_line_per_day"]:
            waiting.append(f"{line} has had {runs_today} runs today (the limit)")
            continue
        if failed_today >= limits["max_failed_runs_per_line_per_day"]:
            waiting.append(f"{line} has had {failed_today} runs today that ended without a "
                           "summary (the limit); see their logs")
            continue
        last = conn.execute("SELECT started FROM runs WHERE program = ? AND line = ? "
                            "ORDER BY id DESC LIMIT 1", (program.name, line)).fetchone()
        if last:
            gap = timedelta(minutes=limits["min_minutes_between_runs"])
            next_start = datetime.fromisoformat(last["started"]) + gap
            if at < next_start:
                waiting.append(f"{line} waits until {clock(program, next_start)} (minimum gap)")
                continue
        ready.append(Due(line, events))
    conn.commit()
    return ready, waiting


def clock(program: Program, moment: datetime) -> str:
    """A time of day in the program's timezone, for notes the scientist reads."""
    return f"{moment.astimezone(ZoneInfo(program.timezone)):%H:%M %Z}"


def where(program: Program, tree: Program) -> str:
    """How a prompt names the run's folder: the line's own worktree."""
    if tree.repo == program.repo:
        return f"Work in {program.repo}"
    return (f"Work in {tree.repo}: it is this line's own git worktree of {program.repo}, on "
            f"branch {lines.branch(tree_line(tree))}. Everything CLAUDE.md and the other "
            f"files say about {program.repo} applies to this folder instead: read, write and "
            "commit here, and leave the other folder alone")


def tree_line(tree: Program) -> str:
    return tree.repo.name.rsplit(".", 1)[-1]


def prompts(program: Program, line: str, stem: str, events: list,
            tree: Program | None = None) -> tuple[str, str, list[str]]:
    tree = tree or program
    reasons = [e["detail"] for e in events]
    instruction = next((e["detail"] for e in reversed(events) if e["kind"] == "instruction"),
                       None)
    worker = [f"Run line {line} of the {program.name} research program. {where(program, tree)}. "
              f"Read scheduled/line.md and follow it. The always-on service "
              f"started this run, so write your summary to sessions/{stem}.json. What started "
              "it: " + "; ".join(reasons) + ". Other lines may be running at the same "
              "time: in PROGRAM.md only add a section for this run, and don't rewrite text "
              "another line wrote. To be started again at a time you choose "
              "(to collect a job, say), run `program wake --line " + line + " --in-minutes N "
              "--reason ...`."]
    if instruction and instruction.startswith("lines/"):
        worker.append(f"Your instruction is {instruction}, this line's own. If an earlier run "
                      "already answered it, deal only with what is new.")
    elif instruction:
        worker.append(f"Your part of the instruction is the `## {line}` section of "
                      f"{instruction}, with anything above the first line section. If an "
                      "earlier run already answered it, deal only with what is new.")
    elif any(e["kind"] == "start" for e in events):
        worker.append(f"{program.scientist} asked for this run without a new instruction: carry "
                      "the line on from its progress notes.")
    else:
        worker.append("There is no new instruction for this line: deal with what started the "
                      "run, and if nothing needs doing, say so in the summary and stop.")
    followed = instruction or "none: started by the service for " + ", ".join(
        sorted({e["kind"] for e in events}))
    worker.append(f'Set "instruction" in the summary to "{followed}".')
    checker = (f"Check line {line}'s run of the {program.name} research program. "
               f"{where(program, tree)}. Read scheduled/line-checker.md and follow it for "
               f"sessions/{stem}.json, and write your review to reviews/{stem}.json.")
    return " ".join(worker), checker, reasons


@dataclass
class Started:
    item: Due
    tree: Program
    run_id: int
    stem: str
    day: date
    worker: str
    checker: str
    reasons: list[str]


def begin(conn, program: Program, item: Due, at: datetime) -> Started | str:
    """Bring the line's worktree up to date with main and record the run.
    Returns the run, or why the line can't start (the line is then paused)."""
    tree, problem = lines.start(program, item.line)
    if problem:
        return problem
    day = today(program, at)
    count = conn.execute("SELECT COUNT(*) FROM runs WHERE program = ? AND line = ? AND day = ?",
                         (program.name, item.line, day.isoformat())).fetchone()[0]
    stem = f"{day.isoformat()}-{item.line}" + (f"-{count + 1}" if count else "")
    worker, checker, reasons = prompts(program, item.line, stem, item.events, tree)
    cur = conn.execute("INSERT INTO runs (program, line, day, stem, started, reasons) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (program.name, item.line, day.isoformat(), stem, at.isoformat(),
                        json.dumps(reasons)))
    conn.executemany("UPDATE events SET status = 'done', run = ? WHERE id = ?",
                     [(cur.lastrowid, e["id"]) for e in item.events])
    conn.commit()
    return Started(item, tree, cur.lastrowid, stem, day, worker, checker, reasons)


def work(program: Program, started: Started):
    """The run itself, in its worktree: the worker, then the checker. Runs in
    a thread of its own; touches no database."""
    logs = Path(program.data_dir).expanduser() / "logs" / "service" / started.day.isoformat()
    return run_line(started.tree, started.day, started.item.line, started.reasons, None, logs,
                    stem=started.stem, worker_text=started.worker, checker_text=started.checker,
                    before_check=lambda: lines.refresh(program, started.item.line))


def end(conn, program: Program, started: Started, result, go_on: bool) -> list[str]:
    """Record the run, then merge a clean run into main and push; a run that
    can't be merged pauses its line. Returns notes."""
    conn.execute("UPDATE runs SET ended = ?, summary = ?, review = ?, problems = ?, counted = ? "
                 "WHERE id = ?",
                 (now_utc().isoformat(), result.summary, result.review,
                  json.dumps(result.problems), 1 if result.summary else 0, started.run_id))
    conn.commit()
    line = started.item.line
    if not result.summary:
        limit = usage_limit(program, started)
        if limit:
            # Not the line's doing: its events wait, and so does the whole program.
            conn.execute("UPDATE events SET status = 'pending', run = NULL WHERE run = ?",
                         (started.run_id,))
            conn.execute("INSERT OR REPLACE INTO holds (program, until, reason) VALUES (?, ?, ?)",
                         (program.name, limit.isoformat(), "waiting for the Claude usage limit"))
            conn.commit()
    notes = [f"ran {started.stem}: {result.minutes:.0f} min, summary {result.summary or 'none'}, "
             f"review {result.review or 'none'}"
             + (f"; problems: {'; '.join(result.problems)}" if result.problems else "")]
    if not go_on:
        pause_line(conn, program, line, f"its run {started.stem} left the line's worktree in a "
                                        "state the next run can't start from; see its logs")
        return notes + [f"paused {line}"]
    problem = lines.finish(program, line)
    if problem:
        pause_line(conn, program, line, problem)
        return notes + [f"paused {line}: {problem}"]
    return notes


LIMIT = re.compile(r"hit your limit.*?resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)",
                   re.IGNORECASE | re.DOTALL)


def usage_limit(program: Program, started: Started) -> datetime | None:
    """If the run's worker stopped at the Claude usage limit, when the limit
    resets (an hour from now if the message doesn't say)."""
    log = (Path(program.data_dir).expanduser() / "logs" / "service" / started.day.isoformat()
           / f"{started.stem}-worker.log")
    text = log.read_text(errors="replace") if log.exists() else ""
    if "hit your limit" not in text.lower():
        return None
    now = now_utc()
    found = LIMIT.search(text)
    if not found:
        return now + timedelta(hours=1)
    hour, minute, half, zone = int(found[1]) % 12, int(found[2] or 0), found[3].lower(), found[4]
    try:
        there = now.astimezone(ZoneInfo(zone.strip()))
    except (KeyError, ValueError):
        return now + timedelta(hours=1)
    reset = there.replace(hour=hour + (12 if half == "pm" else 0), minute=minute, second=0)
    return reset if reset > there else reset + timedelta(days=1)


def merge_unmerged(conn, program: Program) -> list[str]:
    """Merge finished work left on a line's branch into main: a run whose line
    was paused after it, or one the service was stopped during the merge of.
    Paused lines wait for their resume."""
    held = {r["line"] for r in conn.execute("SELECT line FROM paused_lines WHERE program = ?",
                                            (program.name,))}
    notes = []
    for line in lines.unmerged(program):
        if line in held:
            continue
        _, problem = lines.start(program, line)
        problem = problem or lines.finish(program, line)
        if problem:
            pause_line(conn, program, line, problem)
            notes.append(f"paused {line}: {problem}")
        else:
            notes.append(f"merged {line}'s earlier work into main")
    return notes


def run(conn, program: Program, item: Due, at: datetime) -> tuple[bool, str]:
    """Run one line on its own (begin, work, end). Returns whether the
    program can go on, and a note."""
    started = begin(conn, program, item, at)
    if isinstance(started, str):
        pause_line(conn, program, item.line, started)
        return True, f"paused {item.line}: {started}"
    result, go_on = work(program, started)
    return True, "; ".join(end(conn, program, started, result, go_on))


def pause(conn, program: Program, reason: str) -> None:
    conn.execute("INSERT OR REPLACE INTO paused (program, at, reason) VALUES (?, ?, ?)",
                 (program.name, now_utc().isoformat(), reason))
    conn.commit()


def pause_line(conn, program: Program, line: str, reason: str) -> None:
    conn.execute("INSERT OR REPLACE INTO paused_lines (program, line, at, reason) "
                 "VALUES (?, ?, ?, ?)", (program.name, line, now_utc().isoformat(), reason))
    conn.commit()


def resume(conn, name: str) -> bool:
    """Resume a program (``sequel``) or one of its lines (``sequel:L0019``)."""
    program, _, line = name.partition(":")
    if line:
        cur = conn.execute("DELETE FROM paused_lines WHERE program = ? AND line = ?",
                           (program, line))
    else:
        cur = conn.execute("DELETE FROM paused WHERE program = ?", (program,))
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
    with lines.MERGE_LOCK:  # dashboard actions commit to this clone too
        reason = sync(program)
        if reason:
            pause(conn, program, reason)
            return [f"paused: {reason}"]
        synced = sync_template(program)
        try:
            notes = collect(conn, program, today(program, at))
        except ValueError as error:  # reviews or the ledger don't validate
            pause(conn, program, str(error))
            return [f"paused: {error}"]
        if synced:
            notes.insert(0, "synced from RoboRobo: " + ", ".join(synced))
        notes += merge_unmerged(conn, program)
        reason = sync(program)  # push the job records collect committed, and merges
        if reason:
            pause(conn, program, reason)
            return notes + [f"paused: {reason}"]
    try:
        ready, waiting = due(conn, program, at)
    except ValueError as error:  # the ledger doesn't validate
        pause(conn, program, str(error))
        return notes + [f"paused: {error}"]
    notes += [f"waiting: {w}" for w in waiting]
    batch = []
    for item in ready[:max(1, int(program.service["max_parallel_lines"]))]:
        started = begin(conn, program, item, now_utc())
        if isinstance(started, str):
            pause_line(conn, program, item.line, started)
            notes.append(f"paused {item.line}: {started}")
        else:
            batch.append(started)
    # The runs go at once, each in its own worktree; recording, merging into main
    # and pushing happen here, one run at a time.
    with ThreadPoolExecutor(max_workers=max(1, len(batch))) as pool:
        futures = [(started, pool.submit(work, program, started)) for started in batch]
        for started, future in futures:
            result, go_on = future.result()
            with lines.MERGE_LOCK:
                notes += end(conn, program, started, result, go_on)
                reason = sync(program)
            if reason:
                pause(conn, program, reason)
                notes.append(f"paused: {reason}")
                return notes
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


def roborobo_behind() -> bool:
    """Whether RoboRobo's own checkout is behind its remote branch."""
    try:
        git(ROOT, "fetch", "-q", "origin")
        branch = git(ROOT, "rev-parse", "--abbrev-ref", "HEAD").strip()
        return int(git(ROOT, "rev-list", "--count", f"HEAD..origin/{branch}").strip()) > 0
    except RuntimeError as error:  # the remote unreachable: tried again next look
        print(f"couldn't check RoboRobo for updates: {error}", flush=True)
        return False


def serve(repos: list[Path], state: Path, poll_minutes: float, once: bool = False,
          dashboard_host: str = "127.0.0.1", dashboard_port: int = 8080,
          api_port: int = 0) -> None:
    state = state.expanduser()
    conn = connect(state / "service.db")
    # Ledger ids come from here, so lines running at once never take the same one
    # (records.next_id); forwarded commands inherit it.
    os.environ["ROBOROBO_IDS"] = str(state / "service.db")
    if api_port:
        service_api.serve("0.0.0.0", api_port)
        print(f"agent pods' commands on port {api_port}", flush=True)
    if dashboard_port:
        dashboard.serve_http(state, dashboard_host, dashboard_port, repos)
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
        (state / "looks.json").write_text(json.dumps({"looks": looks, "errors": errors}))
        dashboard.write(state, conn, programs, looks, errors)
        for name, notes in [*looks.items(), *errors.items()]:
            for note in notes:
                print(f"{now_utc():%Y-%m-%d %H:%M} {name}: {note}", flush=True)
        if once:
            return
        if os.environ.get("ROBOROBO_SELF_UPDATE") and roborobo_behind():
            # No run is in progress here. Exit so the pod restarts: its entrypoint
            # updates RoboRobo, and the next look syncs each program's rendered files.
            print("RoboRobo has changed on its remote; restarting to update", flush=True)
            sys.exit(0)
        # Until the next poll, or at once when the scientist acts from the dashboard.
        actions.WAKE.wait(poll_minutes * 60)
        actions.WAKE.clear()
