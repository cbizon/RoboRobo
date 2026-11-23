"""Checks on the agent's records: the ledger, session summaries, reviews,
and budgets."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

import jsonschema
import yaml
from referencing import Registry, Resource

from roborobo import SCHEMAS
from roborobo.config import LEDGER, Program
from roborobo.gitutil import has_head, history, show


@cache
def registry() -> Registry:
    resources = []
    for path in SCHEMAS.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@cache
def validator(name: str):
    schema = json.loads((SCHEMAS / name).read_text())
    return jsonschema.Draft202012Validator(
        schema, registry=registry(), format_checker=jsonschema.FormatChecker())


def schema_errors(document: object, schema: str) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.message}"
            for e in validator(schema).iter_errors(document)]


def append_only(program: Program, rel: str) -> list[str]:
    """Each committed version of a ledger file, and the working copy, must
    extend the one before it byte for byte."""
    if not has_head(program.repo):
        return []
    versions = [(commit[:8], show(program.repo, commit, rel) or b"")
                for commit in history(program.repo, rel)]
    versions.append(("working tree", (program.repo / rel).read_bytes()))
    return [f"{rel}: {name} rewrites earlier lines (the ledger is append-only)"
            for (_, before), (name, after) in zip(versions, versions[1:])
            if not after.startswith(before)]


def load_ledger(program: Program) -> tuple[dict[str, list[dict]], list[str]]:
    records: dict[str, list[dict]] = {}
    problems = []
    for filename, (schema, id_field, letter) in LEDGER.items():
        rel = f"ledger/{filename}"
        path = program.repo / rel
        if not path.exists():
            problems.append(f"{rel} is missing")
            continue
        problems += append_only(program, rel)
        records[filename] = []
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                problems.append(f"{rel}:{number}: blank line")
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                problems.append(f"{rel}:{number}: not JSON ({error.msg})")
                continue
            problems += [f"{rel}:{number}: {e}" for e in schema_errors(record, schema)]
            if not str(record.get(id_field, "")).startswith(letter):
                problems.append(f"{rel}:{number}: {id_field} should start with {letter}")
            records[filename].append(record)
    return records, problems


def ledger_consistency(records: dict[str, list[dict]]) -> list[str]:
    problems = []
    seen: dict[str, str] = {}
    for record in records.get("experiments.jsonl", []):
        eid = record["experiment_id"]
        if record["record"] == "registration":
            if eid in seen:
                problems.append(f"{eid} is registered twice; supersede it with a new id")
            if "supersedes" in record and record["supersedes"] not in seen:
                problems.append(f"{eid} supersedes {record['supersedes']}, which isn't registered earlier")
            seen[eid] = "registered"
        elif seen.get(eid) != "registered":
            problems.append(f"{eid} has an outcome without an earlier registration"
                            if eid not in seen else f"{eid} has a second outcome")
        else:
            seen[eid] = "done"
    problems += line_consistency(records, seen)
    for filename, id_field, first in (("claims.jsonl", "claim_id", "planned"),
                                      ("blockers.jsonl", "blocker_id", "open")):
        started = set()
        for record in records.get(filename, []):
            rid = record[id_field]
            if rid not in started and record["status"] != first:
                problems.append(f"{rid}: its first record must be '{first}'")
            started.add(rid)
    decided = set()
    for record in records.get("decisions.jsonl", []):
        if "supersedes" in record and record["supersedes"] not in decided:
            problems.append(f"{record['decision_id']} supersedes {record['supersedes']}, "
                            "which isn't recorded earlier")
        decided.add(record["decision_id"])
    return problems


def line_consistency(records: dict[str, list[dict]], registered: dict[str, str]) -> list[str]:
    """Lines are opened once, before anything refers to them, and once the
    program has lines every new experiment belongs to one."""
    problems = []
    line_records = records.get("lines.jsonl", [])
    opened: set[str] = set()
    for record in line_records:
        lid = record["line_id"]
        if record["record"] == "open":
            if lid in opened:
                problems.append(f"{lid} is opened twice")
            opened.add(lid)
        elif lid not in opened:
            problems.append(f"{lid} has a {record['record']} before it is opened")
        for placed in record.get("experiments", []):
            if placed["experiment_id"] not in registered:
                problems.append(f"{lid} places {placed['experiment_id']}, which isn't registered")
    for record in line_records:
        for other in [record.get("parent"), *record.get("feeds", [])]:
            if other and other not in opened:
                problems.append(f"{record['line_id']} refers to {other}, which isn't a line")
    if line_records:
        since = min(datetime.fromisoformat(r["at"]) for r in line_records)
        for record in records.get("experiments.jsonl", []):
            if record["record"] != "registration" or datetime.fromisoformat(record["at"]) < since:
                continue
            if "line_id" not in record:
                problems.append(f"{record['experiment_id']} has no line_id; every experiment "
                                "belongs to a line of investigation")
            elif record["line_id"] not in opened:
                problems.append(f"{record['experiment_id']} names {record['line_id']}, "
                                "which isn't a line")
    return problems


def latest(records: list[dict], id_field: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for record in records:
        out[record[id_field]] = record
    return out


def documents(program: Program, folder: str, schema: str) -> tuple[list[tuple[str, dict]], list[str]]:
    found, problems = [], []
    for path in sorted((program.repo / folder).glob("*.json")):
        rel = path.relative_to(program.repo).as_posix()
        try:
            document = json.loads(path.read_text())
        except json.JSONDecodeError as error:
            problems.append(f"{rel}: not JSON ({error.msg})")
            continue
        problems += [f"{rel}: {e}" for e in schema_errors(document, schema)]
        found.append((rel, document))
    return found, problems


def blockers_carried(records: dict[str, list[dict]], summaries: list[tuple[str, dict]]) -> list[str]:
    """Every open blocker is listed by the latest night's summaries: by the
    one summary of a whole-program session, or by at least one of the
    night's line summaries."""
    if not summaries:
        return []
    night = max(summary["date"] for _, summary in summaries)
    tonight = [(rel, summary) for rel, summary in summaries if summary["date"] == night]
    listed = {bid for _, summary in tonight for bid in summary.get("open_blockers", [])}
    still_open = {bid for bid, r in latest(records.get("blockers.jsonl", []), "blocker_id").items()
                  if r["status"] in ("open", "escalated")}
    missing = sorted(still_open - listed)
    where = " or ".join(rel for rel, _ in tonight)
    return [f"{where} doesn't list open blockers {', '.join(missing)}"] if missing else []


LOCAL = "local"  # the computer sessions run on; other places are program.toml hosts
SUMMED = ("week", "program")


def budgets(program: Program, records: dict[str, list[dict]],
            now: datetime | None = None) -> tuple[list[str], list[str]]:
    """Check budgets.yaml against its schema and the program's hosts, and sum
    spending from the ledger for limits per week and per program. Limits per
    instant and per run are the agent's to watch; they are reported, not
    summed."""
    path = program.repo / "budgets.yaml"
    if not path.exists():
        return ["budgets.yaml is missing"], []
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        return [f"budgets.yaml is not valid YAML: {error}"], []
    errors = schema_errors(document, "budgets.schema.json")
    if errors:
        old = isinstance(document, dict) and any(
            isinstance(spec, dict) and "limit" in spec
            for spec in (document.get("budgets") or {}).values())
        hint = (" (it uses the old {limit, period} form; each resource is now a list of "
                "limits with max or min, per and on)") if old else ""
        return [f"budgets.yaml{hint}: {e}" for e in errors], []
    places = {LOCAL, *program.hosts}
    now = now or datetime.now(UTC)
    outcomes = [r for r in records.get("experiments.jsonl", []) if r["record"] == "outcome"]
    problems, report = [], []
    for resource, limits in (document["budgets"] or {}).items():
        for limit in limits:
            where = limit["where"]
            unknown = sorted(set(where) - places)
            if unknown:
                problems.append(f"budgets.yaml {resource}: where names {', '.join(unknown)}, "
                                f"which is neither {LOCAL} nor a host in program.toml")
                continue
            bound, value = ("at most", limit["max"]) if "max" in limit else ("at least", limit["min"])
            places_named = ", ".join(where)
            if limit["per"] not in SUMMED:
                report.append(f"{resource}: {bound} {value:g} per {limit['per']} on {places_named} "
                              "(watched by the agent, not summed from the ledger)")
                continue
            missing = sorted(places - set(where))
            if missing:
                problems.append(f"budgets.yaml {resource}: a limit per {limit['per']} must "
                                f"cover every place ({', '.join(sorted(places))}); cost "
                                f"records don't say where they were spent, so one that "
                                f"leaves out {', '.join(missing)} can't be checked")
                continue
            since = now - timedelta(days=7) if limit["per"] == "week" else None
            spent = sum(r["cost_actual"].get(resource, 0.0) for r in outcomes
                        if since is None or datetime.fromisoformat(r["at"]) >= since)
            share = spent / value if value else float("inf")
            report.append(f"{resource}: {spent:g} of {value:g} per {limit['per']} ({share:.0%})")
            if share > 1.0:
                problems.append(f"{resource} is over budget: {spent:g} of {value:g} "
                                f"per {limit['per']}")
    return problems, report


def next_id(program: Program, letter: str) -> str:
    """The next free ledger id. Lines running in parallel (roborobo.lines) each
    see only their own branch's ledger, so in the service ids come from its
    database (``ROBOROBO_IDS``): the larger of the highest id in this ledger
    and the last one handed out, reserved atomically."""
    for filename, (_, id_field, prefix) in LEDGER.items():
        if prefix == letter:
            path = program.repo / "ledger" / filename
            numbers = [int(json.loads(line)[id_field][1:])
                       for line in path.read_text().splitlines() if line.strip()]
            number = max(numbers, default=0) + 1
            database = os.environ.get("ROBOROBO_IDS")
            if database:
                number = reserve(Path(database), program.name, letter, number)
            return f"{letter}{number:04d}"
    raise ValueError(f"no ledger uses ids starting with {letter!r}; use one of E, C, D, B, L, T, A")


def reserve(database: Path, program: str, letter: str, at_least: int) -> int:
    """Hand out the next id number for a program and letter, never one handed
    out before, and never below ``at_least``."""
    conn = sqlite3.connect(database, timeout=60, isolation_level=None)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS ids (program TEXT, letter TEXT, last INTEGER, "
                     "PRIMARY KEY (program, letter))")
        conn.execute("BEGIN IMMEDIATE")  # one reservation at a time, across processes
        row = conn.execute("SELECT last FROM ids WHERE program = ? AND letter = ?",
                           (program, letter)).fetchone()
        number = max(at_least, (row[0] + 1) if row else 0)
        conn.execute("INSERT OR REPLACE INTO ids (program, letter, last) VALUES (?, ?, ?)",
                     (program, letter, number))
        conn.execute("COMMIT")
        return number
    finally:
        conn.close()
