"""What the scientist can do from the dashboard.

The dashboard is reachable by anything in the cluster, agent pods included,
so every action needs the dashboard token: a secret only the scientist and
the service hold (``ROBOROBO_DASHBOARD_TOKEN``). Without a token configured
there are no actions and the page is read-only.

Actions, each a commit on the program's main branch made under the
repository lock, then pushed:

- ``line-status``: make a line the agent proposed ``active`` or drop it;
  pause, close or reopen a line, with a comment kept as a note saying why;
- ``instruct``: add to a line's own instruction
  (``lines/<line>/instruction.md``), which starts the line; answering one of
  a run's questions is an instruction that quotes the question;
- ``resume``: let a paused program or line run again.

Records these write are marked ``by: human``. That is as strong as the
token: an approval of the scientist's files still needs the scientist's
signature (``program approve``), which is not available here.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from roborobo import config, lines
from roborobo.config import Program
from roborobo.gitutil import git
from roborobo.mapview import fold_lines
from roborobo.records import schema_errors

LINE = re.compile(r"L\d{4}")
MAX_TEXT = 8000


# Set by an action so the service looks now instead of at its next poll.
WAKE = threading.Event()


class Refused(Exception):
    """An action that isn't allowed, with the reason to show."""


def token() -> str | None:
    return os.environ.get("ROBOROBO_DASHBOARD_TOKEN") or None


def allowed(given: str | None) -> bool:
    expected = token()
    return bool(expected and given) and hmac.compare_digest(expected.encode(), given.encode())


def find(repos: list[Path], name: str) -> Program:
    for repo in repos:
        program = config.load(repo)
        if program.name == name:
            return program
    raise Refused(f"{name!r} isn't a program this service runs")


def line_of(program: Program, lid: str) -> dict:
    if not LINE.fullmatch(lid or ""):
        raise Refused(f"{lid!r} isn't a line id")
    path = program.repo / "ledger" / "lines.jsonl"
    folded = fold_lines([json.loads(row) for row in path.read_text().splitlines() if row.strip()])
    if lid not in folded:
        raise Refused(f"{lid} isn't a line of {program.name}")
    return folded[lid]


def now(program: Program) -> datetime:
    return datetime.now(ZoneInfo(program.timezone)).replace(microsecond=0)


def commit(program: Program, paths: list[str], message: str) -> None:
    from roborobo import service
    git(program.repo, "add", "--", *paths)
    git(program.repo, "commit", "-q", "-m", message, "--", *paths)
    problem = service.sync(program)
    if problem:
        raise Refused(f"recorded, but not pushed: {problem}")


# What the scientist may make of a line, from each status.
MOVES = {"proposed": {"active", "dropped"}, "active": {"paused", "closed"},
         "paused": {"active", "closed"}, "closed": {"active"}, "dropped": {"active"}}
DONE = {"active": "made active", "dropped": "dropped", "paused": "paused", "closed": "closed"}


def line_status(program: Program, lid: str, status: str, comment: str = "") -> str:
    """Change a line's status: approve or drop a proposed line, pause, close or
    reopen one. Closing, pausing and dropping take a comment, kept as the
    scientist's note on the line, so the record says why."""
    line = line_of(program, lid)
    now_is = line.get("status", "active")
    if status not in MOVES.get(now_is, set()):
        can = " or ".join(sorted(MOVES.get(now_is, set()))) or "nothing"
        raise Refused(f"{lid} is {now_is}; from there it can be made {can}")
    comment = (comment or "").strip()
    if status in ("closed", "paused", "dropped") and now_is != "proposed" and not comment:
        raise Refused("say why, so the record has it")
    if len(comment) > MAX_TEXT:
        raise Refused(f"that is over {MAX_TEXT} characters")
    at = now(program)
    base = {"line_id": lid, "at": at.isoformat(), "by": "human", "night": at.date().isoformat()}
    records = [{"record": "update", **base, "status": status}]
    if comment:
        records.append({"record": "note", **base,
                        "text": f"{DONE[status].capitalize()} by {program.scientist}: {comment}"})
    for record in records:
        errors = schema_errors(record, "line.schema.json")
        if errors:
            raise Refused("that doesn't fit a line record: " + "; ".join(errors))
    with (program.repo / "ledger" / "lines.jsonl").open("a") as ledger:
        for record in records:
            ledger.write(json.dumps(record) + "\n")
    commit(program, ["ledger/lines.jsonl"],
           f"{lid} {DONE[status]} by {program.scientist}, from the dashboard")
    return f"{lid} is {DONE[status]}." + (" Your comment is saved as a note on the line."
                                           if comment else "")


def instruct(program: Program, lid: str, text: str, question: str = "") -> str:
    line_of(program, lid)
    text = (text or "").strip()
    if not text:
        raise Refused("nothing was written")
    if len(text) > MAX_TEXT or len(question) > MAX_TEXT:
        raise Refused(f"that is over {MAX_TEXT} characters; write it in the repository instead")
    path = program.repo / "lines" / lid / "instruction.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    at = now(program)
    entry = [f"## {at:%Y-%m-%d %H:%M}, {program.scientist}, from the dashboard", ""]
    if question.strip():
        entry += ["You asked:", "", *(f"> {row}" for row in question.strip().splitlines()), "",
                  "Answer:", ""]
    entry += [text, ""]
    before = path.read_text().rstrip("\n") + "\n\n" if path.exists() else (
        f"# Instructions for {lid}\n\nNewest last. {program.scientist} writes this file.\n\n")
    path.write_text(before + "\n".join(entry))
    rel = path.relative_to(program.repo).as_posix()
    what = "answered a question of" if question.strip() else "instructed"
    commit(program, [rel], f"{program.scientist} {what} {lid}, from the dashboard")
    return (f"Your instruction for {lid} is recorded. The line starts within a minute, unless "
            "it is waiting on a limit; its row shows where it is.")


def resume(state: Path, program: Program, lid: str = "") -> str:
    from roborobo import service
    if lid:
        line_of(program, lid)
    name = f"{program.name}:{lid}" if lid else program.name
    conn = service.connect(state / "service.db")
    try:
        if not service.resume(conn, name):
            raise Refused(f"{name} isn't paused")
    finally:
        conn.close()
    return f"{name} is resumed and will run again within a minute."


def perform(state: Path, repos: list[Path], form: dict[str, str]) -> str:
    """Carry out one action from the dashboard's form. Raises Refused with the
    reason if it isn't allowed."""
    if not token():
        raise Refused("this dashboard is read-only: no dashboard token is configured")
    if not allowed(form.get("token")):
        raise Refused("that isn't the dashboard token")
    program = find(repos, form.get("program", ""))
    action = form.get("action")
    with lines.MERGE_LOCK:  # the service's own commits, merges and pushes wait, and so do we
        if action == "line-status":
            said = line_status(program, form.get("line", ""), form.get("status", ""),
                               form.get("comment", ""))
        elif action == "instruct":
            said = instruct(program, form.get("line", ""), form.get("text", ""),
                            form.get("question", ""))
        elif action == "resume":
            said = resume(state, program, form.get("line", ""))
        else:
            raise Refused(f"{action!r} isn't something this page can do")
    WAKE.set()
    return said
