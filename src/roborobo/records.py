"""Checks on the agent's records: the ledger, session summaries, reviews,
and budgets."""

from __future__ import annotations

import json
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
    if not summaries:
        return []
    rel, summary = summaries[-1]
    still_open = {bid for bid, r in latest(records.get("blockers.jsonl", []), "blocker_id").items()
                  if r["status"] in ("open", "escalated")}
    missing = sorted(still_open - set(summary.get("open_blockers", [])))
    return [f"{rel} doesn't list open blockers {', '.join(missing)}"] if missing else []


def budgets(program: Program, records: dict[str, list[dict]],
            now: datetime | None = None) -> tuple[list[str], list[str]]:
    """Spending against budgets.yaml for resources summed from the ledger
    (periods 'program' and 'week'); other periods are reported as not
    checked here."""
    path = program.repo / "budgets.yaml"
    if not path.exists():
        return ["budgets.yaml is missing"], []
    limits = (yaml.safe_load(path.read_text()) or {}).get("budgets") or {}
    now = now or datetime.now(UTC)
    outcomes = [r for r in records.get("experiments.jsonl", []) if r["record"] == "outcome"]
    problems, report = [], []
    for resource, spec in limits.items():
        period, limit = spec["period"], float(spec["limit"])
        if period not in ("program", "week"):
            report.append(f"{resource}: per {period}, not summed from the ledger")
            continue
        since = now - timedelta(days=7) if period == "week" else None
        spent = sum(r["cost_actual"].get(resource, 0.0) for r in outcomes
                    if since is None or datetime.fromisoformat(r["at"]) >= since)
        share = spent / limit if limit else float("inf")
        report.append(f"{resource}: {spent:g} of {limit:g} per {period} ({share:.0%})")
        if share > 1.0:
            problems.append(f"{resource} is over budget: {spent:g} of {limit:g} per {period}")
    return problems, report


def next_id(program: Program, letter: str) -> str:
    for filename, (_, id_field, prefix) in LEDGER.items():
        if prefix == letter:
            path = program.repo / "ledger" / filename
            numbers = [int(json.loads(line)[id_field][1:])
                       for line in path.read_text().splitlines() if line.strip()]
            return f"{letter}{max(numbers, default=0) + 1:04d}"
    raise ValueError(f"no ledger uses ids starting with {letter!r}; use one of E, C, D, B")
