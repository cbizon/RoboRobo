"""Data artifacts: what runs produced, where it lives, where it came from, and
what was destroyed.

Everything in a program's data folder is one of four kinds:

- **data**: what runs produce. A data folder is a job's run folder on a
  cluster, or a folder in the data folder here (normally
  ``runs/<experiment>/``). Every data folder is an *artifact*: a record in
  ``ledger/artifacts.jsonl`` and a manifest of its files (sha256, bytes,
  path) in ``ledger/manifests/<id>-<n>.tsv``.
- **scratch**: ``tmp/``, including ``tmp/fetch/``, where ``program fetch``
  puts copies of cluster data to look at. Never recorded; the night runner
  empties ``tmp/fetch/`` after every run.
- **code**: git repositories (method worktrees, clones of other code).
  Versioned already, so not recorded here.
- **operational**: ``jobs/``, ``logs/``, ``map/``, ``mirror/`` (small files
  copied from cluster artifacts) and files directly in ``runs/`` (such as
  ``active.jsonl``).

``program check`` fails on anything in the data folder that is none of these,
and on a recorded local artifact that disappeared without ``program delete``.
Cluster artifacts are recorded by ``program jobs`` when their job ends.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from roborobo.config import Program
from roborobo.records import next_id, schema_errors

LEDGER_FILE = "ledger/artifacts.jsonl"
MANIFESTS = "ledger/manifests"
LOCAL = "local"
OPERATIONAL = {"tmp", "logs", "map", "jobs", "mirror"}
IGNORED = {".DS_Store"}
ARTIFACT_REF = re.compile(r"(A\d{4})(?::(.*))?")
# Cited artifacts can't be deleted: the formal citations are claims and
# experiment outcomes.
CITING = ("ledger/claims.jsonl", "ledger/experiments.jsonl")

# Runs here or on a cluster's login node (Python 3.9 there): prints one line
# per file, "sha256<TAB>bytes<TAB>path relative to ROOT", sorted by path.
MANIFEST_SCRIPT = """\
import hashlib, os, sys
root = sys.argv[1]
targets = sys.argv[2:] or ["."]
rows = []
for target in targets:
    start = os.path.join(root, target)
    if os.path.isfile(start):
        walk = [(os.path.dirname(start), [], [os.path.basename(start)])]
    elif os.path.isdir(start):
        walk = os.walk(start)
    else:
        continue
    for folder, dirs, files in walk:
        for name in files:
            if name == ".DS_Store":
                continue
            full = os.path.join(folder, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            digest = hashlib.sha256()
            with open(full, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            rows.append((os.path.relpath(full, root), digest.hexdigest(), os.path.getsize(full)))
for rel, digest, size in sorted(rows):
    print(digest + "\\t" + str(size) + "\\t" + rel)
"""


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def data_dir(program: Program) -> Path:
    return Path(program.data_dir).expanduser()


# --- the records ---------------------------------------------------------------

def load(program: Program) -> list[dict]:
    path = program.repo / LEDGER_FILE
    if not path.exists():
        raise SystemExit(f"{LEDGER_FILE} is missing; run `program sync`")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def fold(records: list[dict]) -> dict[str, dict]:
    """Each artifact's current state: its created record with later manifests
    applied, ``revisions`` counted, and ``deleted`` set once it is gone."""
    state: dict[str, dict] = {}
    for record in records:
        aid = record["artifact_id"]
        if record["record"] == "created":
            state[aid] = {**record, "revisions": 1}
        elif aid in state:
            if record["record"] == "updated":
                state[aid].update({k: record[k] for k in
                                   ("files", "bytes", "manifest", "manifest_sha256")})
                state[aid]["revisions"] += 1
            else:
                state[aid]["deleted"] = record
    return state


def live(program: Program) -> dict[str, dict]:
    return {aid: a for aid, a in fold(load(program)).items() if "deleted" not in a}


def append(program: Program, record: dict) -> None:
    errors = schema_errors(record, "artifact.schema.json")
    if errors:
        raise RuntimeError(f"artifact record doesn't validate: {'; '.join(errors)}")
    with (program.repo / LEDGER_FILE).open("a") as ledger:
        ledger.write(json.dumps(record) + "\n")


def write_manifest(program: Program, aid: str, revision: int, text: str) -> dict:
    rel = f"{MANIFESTS}/{aid}-{revision}.tsv"
    path = program.repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    rows = [line.split("\t") for line in text.splitlines() if line]
    return {"files": len(rows), "bytes": sum(int(row[1]) for row in rows), "manifest": rel,
            "manifest_sha256": hashlib.sha256(text.encode()).hexdigest()}


def local_manifest(root: Path, targets: list[str] = ()) -> str:
    done = subprocess.run([sys.executable, "-", str(root), *targets], input=MANIFEST_SCRIPT,
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"making the manifest of {root} failed: {done.stderr.strip()}")
    return done.stdout


def registered_line(program: Program, experiment: str | None) -> str | None:
    if not experiment:
        return None
    path = program.repo / "ledger" / "experiments.jsonl"
    for line in path.read_text().splitlines():
        record = json.loads(line) if line.strip() else {}
        if record.get("experiment_id") == experiment and record.get("record") == "registration":
            return record.get("line_id")
    return None


# --- local artifacts ------------------------------------------------------------

def local_rel(program: Program, path: Path | str) -> str:
    """A path in the data folder, relative to it; fails for anything else."""
    root = data_dir(program).resolve()
    full = Path(path).expanduser()
    full = (full if full.is_absolute() else root / full).resolve()
    if not full.is_relative_to(root) or full == root:
        raise SystemExit(f"{full} is not inside the program's data folder {root}")
    return full.relative_to(root).as_posix()


def check_data_path(program: Program, rel: str) -> None:
    root = data_dir(program)
    top = rel.split("/")[0]
    if top in OPERATIONAL:
        raise SystemExit(f"{rel} is in {top}/, which holds {'scratch' if top == 'tmp' else 'operational state'}, "
                         "not data")
    parts = rel.split("/")
    for depth in range(1, len(parts) + 1):
        if (root.joinpath(*parts[:depth]) / ".git").exists():
            raise SystemExit(f"{rel} is in a git repository, which is code, not data")


def record_local(program: Program, path: Path | str, experiment: str | None = None,
                 line: str | None = None, inputs: list[str] = (), note: str | None = None,
                 by: str = "agent", provenance: str = "recorded") -> tuple[str, str]:
    """Record a local data folder (or a new manifest for one already recorded).
    Returns its id and whether it was created or updated."""
    rel = local_rel(program, path)
    full = data_dir(program) / rel
    if not full.exists():
        raise SystemExit(f"{full} does not exist")
    check_data_path(program, rel)
    current = live(program)
    same = [a for a in current.values() if a["host"] == LOCAL and a["path"] == rel]
    overlap = [a for a in current.values() if a["host"] == LOCAL and a["path"] != rel and
               (rel.startswith(a["path"] + "/") or a["path"].startswith(rel + "/"))]
    if overlap:
        raise SystemExit(f"{rel} overlaps {', '.join(a['artifact_id'] + ' (' + a['path'] + ')' for a in overlap)}; "
                         "an artifact can't contain another")
    text = local_manifest(full)
    if same:
        artifact = same[0]
        aid = artifact["artifact_id"]
        record = {"record": "updated", "artifact_id": aid, "at": now(), "by": by,
                  **write_manifest(program, aid, artifact["revisions"] + 1, text)}
        if note:
            record["note"] = note
        append(program, record)
        return aid, "updated"
    match = re.match(r"runs/(E\d{4})(/|$)", rel)
    experiment = experiment or (match[1] if match else None)
    line = line or registered_line(program, experiment)
    aid = next_id(program, "A")
    record = {"record": "created", "artifact_id": aid, "at": now(), "by": by, "host": LOCAL,
              "path": rel, "provenance": provenance}
    if experiment:
        record["experiment_id"] = experiment
    if line:
        record["line_id"] = line
    if inputs:
        record["inputs"] = [resolve_input(program, ref)["record"] for ref in inputs]
    if note:
        record["note"] = note
    record.update(write_manifest(program, aid, 1, text))
    append(program, record)
    return aid, "created"


def unrecorded(program: Program, artifacts: dict[str, dict] | None = None) -> list[str]:
    """Paths in the data folder that are not data, scratch, code or
    operational state: the folders and files no artifact covers. Doesn't
    descend into an uncovered folder; reports it whole."""
    root = data_dir(program)
    if not root.exists():
        return []
    artifacts = live(program) if artifacts is None else artifacts
    covered = {a["path"] for a in artifacts.values() if a["host"] == LOCAL}
    found = []

    def walk(folder: Path, rel: str) -> None:
        for entry in sorted(os.scandir(folder), key=lambda e: e.name):
            path = f"{rel}/{entry.name}" if rel else entry.name
            if entry.name in IGNORED or path in covered:
                continue
            if not rel and entry.name in OPERATIONAL:
                continue
            if rel == "runs" and not entry.is_dir(follow_symlinks=False):
                continue  # operational files such as active.jsonl
            if (Path(entry.path) / ".git").exists():
                continue  # code: a git repository, or a link to one
            if entry.is_dir(follow_symlinks=False):
                holds_code = any((Path(child.path) / ".git").exists()
                                 for child in os.scandir(entry.path) if child.is_dir())
                if path == "runs" or holds_code or any(c.startswith(path + "/") for c in covered):
                    walk(Path(entry.path), path)
                else:
                    found.append(path + "/")
            else:
                found.append(path)

    walk(root, "")
    return found


def summary_of(folder: Path) -> tuple[int, int]:
    files = size = 0
    for current, _, names in os.walk(folder):
        for name in names:
            full = os.path.join(current, name)
            if name in IGNORED or os.path.islink(full):
                continue
            files += 1
            size += os.path.getsize(full)
    return files, size


def check(program: Program, records: list[dict]) -> tuple[list[str], list[str]]:
    """Problems and notes for program check."""
    problems, notes = [], []
    for record in records:
        if "manifest" not in record:
            continue
        path = program.repo / record["manifest"]
        if not path.exists():
            problems.append(f"{record['artifact_id']}: manifest {record['manifest']} is missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != record["manifest_sha256"]:
            problems.append(f"{record['artifact_id']}: manifest {record['manifest']} doesn't "
                            "match its recorded sha256")
    seen: set[str] = set()
    for record in records:
        aid = record["artifact_id"]
        if record["record"] == "created" and aid in seen:
            problems.append(f"{aid} is created twice")
        elif record["record"] != "created" and aid not in seen:
            problems.append(f"{aid} has a {record['record']} record before it is created")
        seen.add(aid)
    current = {aid: a for aid, a in fold(records).items() if "deleted" not in a}
    root = data_dir(program)
    for aid, artifact in current.items():
        if artifact["host"] != LOCAL:
            continue
        folder = root / artifact["path"]
        if not folder.exists():
            problems.append(f"{aid} ({artifact['path']}) is gone from the data folder but has no "
                            "deleted record; delete data with `program delete`")
            continue
        files, size = summary_of(folder) if folder.is_dir() else (1, folder.stat().st_size)
        if (files, size) != (artifact["files"], artifact["bytes"]):
            notes.append(f"{aid} ({artifact['path']}) changed since it was recorded "
                         f"({artifact['files']} files, {artifact['bytes']} bytes; now {files}, "
                         f"{size}); run `program record` on it when it is finished")
    missing = unrecorded(program, current)
    if missing:
        shown = ", ".join(missing[:8]) + (f" and {len(missing) - 8} more" if len(missing) > 8 else "")
        problems.append(f"the data folder has unrecorded data: {shown}. Record data with "
                        "`program record <folder>`; scratch goes in tmp/")
    return problems, notes


# --- inputs, fetch and delete ----------------------------------------------------

def resolve_input(program: Program, ref: str) -> dict:
    """An input given as ``A0123[:sub/path]`` or as a path in the data folder
    inside a recorded artifact. Returns the artifact, the part used, and the
    record to store."""
    current = live(program)
    match = ARTIFACT_REF.fullmatch(ref)
    if match:
        aid, sub = match[1], (match[2] or "").strip("/")
        if aid not in current:
            raise SystemExit(f"{aid} is not a live artifact")
        artifact = current[aid]
    else:
        rel = local_rel(program, ref)
        holders = [a for a in current.values() if a["host"] == LOCAL and
                   (rel == a["path"] or rel.startswith(a["path"] + "/"))]
        if not holders:
            raise SystemExit(f"{rel} is not in a recorded artifact; record it first with "
                             "`program record`")
        artifact = holders[0]
        sub = rel[len(artifact["path"]):].strip("/")
    return {"artifact": artifact, "sub": sub,
            "record": {"artifact_id": artifact["artifact_id"], "path": sub}}


def located(artifact: dict, program: Program, sub: str = "") -> str:
    """Where an artifact (or a part of it) is, on its own host."""
    base = (str(data_dir(program) / artifact["path"]) if artifact["host"] == LOCAL
            else artifact["path"])
    return f"{base}/{sub}" if sub else base


def cited(program: Program, aid: str) -> list[str]:
    pattern = re.compile(rf"\b{aid}\b")
    return [rel for rel in CITING if (program.repo / rel).exists() and
            pattern.search((program.repo / rel).read_text())]


def delete(program: Program, aid: str, reason: str, by: str = "agent") -> str:
    from roborobo import cluster
    current = live(program)
    if aid not in current:
        raise SystemExit(f"{aid} is not a live artifact")
    citing = cited(program, aid)
    if citing:
        raise SystemExit(f"{aid} is cited in {', '.join(citing)}; cited data can't be deleted")
    artifact = current[aid]
    where = located(artifact, program)
    if artifact["host"] == LOCAL:
        path = Path(where)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    else:
        h = cluster.host(program, artifact["host"])
        cluster.remote(h, f"rm -rf {shlex.quote(where)}")
    append(program, {"record": "deleted", "artifact_id": aid, "at": now(), "by": by,
                     "reason": reason})
    return where


def fetch(program: Program, ref: str, max_mb: int | None = None) -> Path:
    """Copy a cluster artifact, or part of one, into tmp/fetch/ to look at."""
    from roborobo import cluster
    match = ARTIFACT_REF.fullmatch(ref)
    if not match:
        raise SystemExit(f"{ref!r}: give an artifact id, optionally with a path in it: "
                         "A0123 or A0123:out/summary.json")
    found = resolve_input(program, ref)
    artifact, sub = found["artifact"], found["sub"]
    if artifact["host"] == LOCAL:
        raise SystemExit(f"{artifact['artifact_id']} is already here: {located(artifact, program, sub)}")
    h = cluster.host(program, artifact["host"])
    source = located(artifact, program, sub)
    limit = h.fetch_max_mb if max_mb is None else max_mb
    size = remote_mb(h, source)
    if size is None:
        raise SystemExit(f"{source} doesn't exist on {h.name}")
    if size > limit:
        raise SystemExit(f"{source} is {size} MB, over the limit of {limit} MB; fetch a part "
                         "of it, run the analysis as a job next to the data, or pass --max-mb")
    dest = data_dir(program) / "tmp" / "fetch" / artifact["artifact_id"] / sub
    dest.parent.mkdir(parents=True, exist_ok=True)
    cluster.rsync([f"{h.ssh}:{source}", str(dest)], f"fetching {source} from {h.name}")
    return dest


def remote_mb(h, path: str) -> int | None:
    from roborobo import cluster
    out = cluster.remote(h, f"if [ -e {shlex.quote(path)} ]; then du -sm {shlex.quote(path)} "
                            "| cut -f1; else echo none; fi").strip()
    return None if out == "none" else int(out)
