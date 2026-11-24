"""What the scientist can do from the dashboard.

The dashboard is reachable by anything in the cluster, agent pods included,
so every action needs the dashboard token: a secret only the scientist and
the service hold (``ROBOROBO_DASHBOARD_TOKEN``). Without a token configured
there are no actions and the page is read-only.

Actions, each a commit on the program's main branch made under the
repository lock, then pushed:

- ``line-status``: make a line the agent proposed ``active``, or drop it;
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


def line_status(program: Program, lid: str, status: str) -> str:
    if status not in ("active", "dropped"):
        raise Refused("a proposed line can be made active or dropped")
    line = line_of(program, lid)
    if line.get("status") != "proposed":
        raise Refused(f"{lid} is {line.get('status')}, not proposed")
    at = now(program)
    record = {"record": "update", "line_id": lid, "at": at.isoformat(), "by": "human",
              "night": at.date().isoformat(), "status": status}
    errors = schema_errors(record, "line.schema.json")
    if errors:
        raise RuntimeError(f"line record doesn't validate: {'; '.join(errors)}")
    with (program.repo / "ledger" / "lines.jsonl").open("a") as ledger:
        ledger.write(json.dumps(record) + "\n")
    done = "made active" if status == "active" else "dropped"
    commit(program, ["ledger/lines.jsonl"],
           f"{lid} {done} by {program.scientist}, from the dashboard")
    return f"{lid} is {done}."


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
    return f"Added to {rel}. {lid} starts at the service's next look, within its limits."


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
    return f"{name} will run again at the service's next look."


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
            return line_status(program, form.get("line", ""), form.get("status", ""))
        if action == "instruct":
            return instruct(program, form.get("line", ""), form.get("text", ""),
                            form.get("question", ""))
        if action == "resume":
            return resume(state, program, form.get("line", ""))
    raise Refused(f"{action!r} isn't something this page can do")
