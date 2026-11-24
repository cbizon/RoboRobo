"""One night of a program, run one line of investigation at a time.

For each line the night's instruction has a section for (``## L0013 ...``),
and each active line whose latest review has a challenge still open, the
runner starts a worker run, then a checker run on what it produced, each a
separate agent process that sees only the repository.  Lines run one after
another, so every run commits its records on ``main`` as sessions did.

Nothing is retried or skipped quietly.  A run that fails, times out or
leaves no summary is reported; a run that leaves the repository dirty or
``program check`` failing stops the night, since the next line would start
from that state.  The night's report goes to ``<data_dir>/logs/<night>/``
and to standard output, and the exit code is 1 if anything went wrong.
"""

from __future__ import annotations

import os
import secrets
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from roborobo import kube, service_api
from roborobo.checks import check_program
from roborobo.config import Program
from roborobo.gitutil import git
from roborobo.mapview import fold_lines
from roborobo.records import documents, load_ledger

LINE_HEADING = re.compile(r"^## (L\d{4})\b", re.MULTILINE)
CHALLENGED = ("doubtful", "broken")


def clock(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def night_of(program: Program, now: datetime) -> date:
    """The evening a night started on: before the session's end, a night
    that crosses midnight is still yesterday's."""
    start, end = clock(program.schedule["session_start"]), clock(program.schedule["session_end"])
    if end <= start and now.time() < end:
        return now.date() - timedelta(days=1)
    return now.date()


def deadline(program: Program, night: date) -> datetime:
    """After this, no new line is started."""
    start, end = clock(program.schedule["session_start"]), clock(program.schedule["session_end"])
    return datetime.combine(night + timedelta(days=1 if end <= start else 0), end)


def instructed_lines(text: str, source: str) -> list[str]:
    """Lines the instruction has a section for, in the order it gives them."""
    found = LINE_HEADING.findall(text)
    twice = sorted({lid for lid in found if found.count(lid) > 1})
    if twice:
        raise ValueError(f"{source}: more than one section for {', '.join(twice)}")
    return found


def challenged_lines(program: Program) -> dict[str, str]:
    """Each line whose latest review has a claim judged doubtful or broken,
    with that review's path."""
    reviews, problems = documents(program, "reviews", "review.schema.json")
    if problems:
        raise ValueError("reviews don't validate:\n  " + "\n  ".join(problems))
    newest: dict[str, tuple[str, dict]] = {}
    for rel, review in reviews:
        if "line_id" in review:
            newest[review["line_id"]] = (rel, review)
    return {lid: rel for lid, (rel, review) in newest.items()
            if any(item["verdict"] in CHALLENGED for item in review["reviews"])}


@dataclass
class Plan:
    night: date
    instruction: str | None
    lines: list[tuple[str, list[str]]] = field(default_factory=list)  # (line, reasons)
    skipped: list[str] = field(default_factory=list)                  # problems


def plan(program: Program, night: date) -> Plan:
    rel = f"instructions/{night.isoformat()}.md"
    path = program.repo / rel
    instructed = instructed_lines(path.read_text(), rel) if path.exists() else []
    records, problems = load_ledger(program)
    if problems:
        raise ValueError("the ledger doesn't validate; run `program check`:\n  "
                         + "\n  ".join(problems))
    status = {lid: line["status"] for lid, line in fold_lines(records["lines.jsonl"]).items()}
    challenged = challenged_lines(program)
    result = Plan(night, rel if path.exists() else None)
    if path.exists() and not instructed:
        result.skipped.append(f"{rel} has no line sections (`## L0001 ...`), so none of it "
                              "runs; see instructions/README.md")
    for lid in instructed:
        if lid not in status:
            result.skipped.append(f"{rel} has a section for {lid}, which isn't a line")
        elif status[lid] != "active":
            result.skipped.append(f"{rel} has a section for {lid}, which is {status[lid]}")
        else:
            reasons = ["instructed"]
            if lid in challenged:
                reasons.append(f"open challenges in {challenged[lid]}")
            result.lines.append((lid, reasons))
    for lid, review in sorted(challenged.items()):
        if lid not in instructed and status.get(lid) == "active":
            result.lines.append((lid, [f"open challenges in {review}"]))
    return result


def worker_prompt(program: Program, night: date, lid: str, reasons: list[str],
                  instruction: str | None) -> str:
    parts = [f"Run line {lid} of the {program.name} research program for the night of "
             f"{night.isoformat()}. Work in {program.repo}: read scheduled/line.md and "
             "follow it."]
    if "instructed" in reasons:
        parts.append(f"Your part of tonight's instruction is the `## {lid}` section of "
                     f"{instruction}, with anything above the first line section.")
    else:
        parts.append("Tonight's instruction has no section for this line; answer the open "
                     "challenges and nothing else.")
    parts += [f"There are {reason}; answer them." for reason in reasons if reason != "instructed"]
    return " ".join(parts)


def checker_prompt(program: Program, night: date, lid: str) -> str:
    return (f"Check line {lid}'s run of the {program.name} research program for the night "
            f"of {night.isoformat()}. Work in {program.repo}: read scheduled/line-checker.md "
            f"and follow it for sessions/{night.isoformat()}-{lid}.json.")


def run_agent(program: Program, prompt: str, minutes: float, log: Path,
              role: str = "agent") -> str | None:
    """Run the configured agent command; return what went wrong, if anything.
    ``[runs] checker`` in program.toml, if set, is the checker's command (a
    different model or harness than the worker's ``agent``). In the service
    (ROBOROBO_RUNNER=kubernetes) the run is a pod of its own, without the
    service's keys; see roborobo.kube."""
    command = [*(program.runs.get(role) or program.runs["agent"]), prompt]
    log.parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ROBOROBO_RUNNER") == "kubernetes":
        token = secrets.token_hex(24)
        service_api.ACTIVE.add(token, program.repo)  # good only for this run's folder
        try:
            return kube.run(kube.in_cluster(), kube.job_name(log.stem), command,
                            str(program.repo), minutes, token, log)
        finally:
            service_api.ACTIVE.remove(token)
            empty_fetched(program)
    try:
        with log.open("w") as out:
            try:
                done = subprocess.run(command, cwd=program.repo, stdin=subprocess.DEVNULL,
                                      stdout=out, stderr=subprocess.STDOUT,
                                      timeout=minutes * 60)
            except subprocess.TimeoutExpired:
                return f"stopped after its limit of {minutes:g} minutes (log: {log})"
            except FileNotFoundError:
                return f"the agent command {command[0]!r} wasn't found"
    finally:
        empty_fetched(program)
    if done.returncode != 0:
        return f"exited with code {done.returncode} (log: {log})"
    return None


def empty_fetched(program: Program) -> None:
    """Copies fetched to look at (``program fetch``) never outlive the run."""
    fetched = Path(program.data_dir).expanduser() / "tmp" / "fetch"
    if fetched.exists():
        shutil.rmtree(fetched)


def uncommitted(program: Program) -> list[str]:
    return [line for line in git(program.repo, "status", "--porcelain").splitlines() if line]


@dataclass
class LineResult:
    line: str
    reasons: list[str]
    problems: list[str] = field(default_factory=list)
    summary: str | None = None
    review: str | None = None
    minutes: float = 0.0


def after_run(program: Program, who: str, expected: str) -> tuple[list[str], bool]:
    """What's wrong after a run, and whether the night can go on."""
    problems = []
    if not (program.repo / expected).exists():
        problems.append(f"the {who} wrote no {expected}")
    dirty = uncommitted(program)
    if dirty:
        problems.append(f"the {who} left uncommitted changes: " + "; ".join(dirty[:5]))
        return problems, False
    failed, _ = check_program(program)
    if failed:
        problems += [f"program check after the {who}: {p}" for p in failed]
        return problems, False
    return problems, True


def checked(program: Program, who: str, expected: str, before_check) -> tuple[list[str], bool]:
    """after_run, once the service has brought the line's worktree up to date
    with main (``before_check`` returns a problem, or None)."""
    problem = before_check() if before_check else None
    if problem:
        return [problem], False
    return after_run(program, who, expected)


def run_line(program: Program, night: date, lid: str, reasons: list[str],
             instruction: str | None, logs: Path, stem: str | None = None,
             worker_text: str | None = None, checker_text: str | None = None,
             before_check=None) -> tuple[LineResult, bool]:
    """A line's worker run, then the checker on what it produced. The night
    runner names files by night (``sessions/<night>-<line>.json``); the
    service passes its own ``stem`` and prompts."""
    result = LineResult(lid, reasons)
    began = datetime.now()
    log_name = stem or lid  # the night's logs sit in a folder per night
    stem = stem or f"{night.isoformat()}-{lid}"
    summary = f"sessions/{stem}.json"
    problem = run_agent(program,
                        worker_text or worker_prompt(program, night, lid, reasons, instruction),
                        float(program.runs["worker_minutes"]), logs / f"{log_name}-worker.log")
    if problem:
        result.problems.append(f"worker {problem}")
    problems, go_on = checked(program, "worker", summary, before_check)
    result.problems += problems
    if (program.repo / summary).exists():
        result.summary = summary
    if go_on and result.summary:
        review = f"reviews/{stem}.json"
        problem = run_agent(program, checker_text or checker_prompt(program, night, lid),
                            float(program.schedule["checker_minutes"]),
                            logs / f"{log_name}-checker.log", role="checker")
        if problem:
            result.problems.append(f"checker {problem}")
        problems, go_on = checked(program, "checker", review, before_check)
        result.problems += problems
        if (program.repo / review).exists():
            result.review = review
    result.minutes = (datetime.now() - began).total_seconds() / 60
    return result, go_on


def report(night_plan: Plan, results: list[LineResult], stopped: str | None,
           not_started: list[str]) -> str:
    out = [f"# Night of {night_plan.night.isoformat()}", ""]
    out.append(f"Instruction: {night_plan.instruction or 'none'}")
    if not night_plan.lines and not night_plan.skipped:
        out.append("No line was instructed and none has open challenges; nothing ran.")
    for problem in night_plan.skipped:
        out.append(f"- skipped: {problem}")
    for r in results:
        out += ["", f"## {r.line} ({', '.join(r.reasons)}; {r.minutes:.0f} min)",
                f"- summary: {r.summary or 'none'}", f"- review: {r.review or 'none'}"]
        out += [f"- problem: {p}" for p in r.problems]
    if stopped:
        out += ["", f"**The night stopped early:** {stopped}"]
    if not_started:
        out += ["", "Not started: " + ", ".join(not_started)]
    return "\n".join(out) + "\n"


def run_night(program: Program, night: date, only: list[str] | None = None,
              dry_run: bool = False, now=datetime.now) -> tuple[int, str]:
    """Run the night; return the exit code and the report.  Lines named in
    `only` are run on request, so the session's end doesn't stop them."""
    night_plan = plan(program, night)
    if only:
        unknown = sorted(set(only) - {lid for lid, _ in night_plan.lines})
        if unknown:
            raise ValueError(f"not planned tonight: {', '.join(unknown)}")
        night_plan.lines = [(lid, why) for lid, why in night_plan.lines if lid in only]
        night_plan.skipped = []  # only the chosen lines are this run's concern
    if dry_run:
        lines = [f"{lid}: {', '.join(why)}" for lid, why in night_plan.lines]
        text = report(night_plan, [], None, []) + "\nWould run:\n" + "\n".join(lines) + "\n"
        return (1 if night_plan.skipped else 0), text

    logs = Path(program.data_dir).expanduser() / "logs" / night.isoformat()
    results: list[LineResult] = []
    stopped = None
    agent = program.runs["agent"][0]
    dirty = uncommitted(program)
    failed, _ = check_program(program)
    if shutil.which(agent) is None:
        stopped = (f"the agent command {agent!r} isn't on this PATH "
                   f"({os.environ.get('PATH', '')}); give its full path as [runs] agent "
                   "in program.toml, or add its folder to PATH where the runner starts")
    elif dirty:
        stopped = "the repository has uncommitted changes: " + "; ".join(dirty[:5])
    elif failed:
        stopped = "program check fails before the first line: " + "; ".join(failed)
    remaining = [] if stopped else list(night_plan.lines)
    end = deadline(program, night)
    while remaining and (only or now() < end):
        lid, reasons = remaining.pop(0)
        result, go_on = run_line(program, night, lid, reasons, night_plan.instruction, logs)
        results.append(result)
        if not go_on:
            stopped = f"{lid} left the repository in a state the next line can't start from"
            break
    not_started = [lid for lid, _ in (night_plan.lines if stopped and not results else remaining)]
    text = report(night_plan, results, stopped, not_started)
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "night.md").write_text(text)
    bad = stopped or not_started or night_plan.skipped or any(r.problems for r in results)
    return (1 if bad else 0), text
