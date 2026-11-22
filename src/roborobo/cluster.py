"""Jobs on Slurm clusters (Hatteras): ``program submit`` and ``program jobs``.

A job runs one command under ``sbatch`` in a run folder on the cluster::

    <workdir>/<program>/<experiment>/<UTC time>/
        code/      the program repository at one commit (the command runs here)
        <tree>/    more checkouts: ``--tree engine=method/nudge``, another
                   repository in the data folder with ``--tree NAME=PATH:REV``,
                   and every
                   uv path source in pyproject.toml (``../research-programs``
                   lands at ``research-programs/``, so the path still resolves)
        in/        files sent with ``--input``
        out/       the job's outputs (``$ROBOROBO_OUT``)
        slurm-<job>.out

Every tree is a real git checkout at a recorded commit, pushed over SSH to a
bare repository under ``<workdir>/git/``: the cluster needs no GitHub access,
and code that asks git for its own commit works.

Hosts are the scientist's: ``[hosts.<name>]`` in ``program.toml``, which is
human-only and approved, so the agent can't add a host or raise its limits.

A job's state lives in ``<data_dir>/jobs/<host>-<jobid>.json``. Each
``program jobs`` syncs ``out/`` (less the host's ``fetch_exclude`` patterns,
such as checkpoints) and the Slurm log to
``<data_dir>/runs/<experiment>/<host>-<jobid>/``, for running jobs too, so
progress is visible. When a job ends, its ``job.json`` (commits, command,
resources, state, CPU-hours) is also written to
``evidence/<experiment>/<host>-<jobid>.json``, so the record in git says
where and how it ran. Every failure (a job that failed, timed out, ran out of
memory or vanished; outputs too big to fetch) is reported and makes the
command exit non-zero. Nothing is retried on its own.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from roborobo.config import Program
from roborobo.gitutil import git, show

ACTIVE = {"PENDING", "RUNNING", "REQUEUED", "RESIZING", "SUSPENDED", "CONFIGURING",
          "COMPLETING", "STAGE_OUT", "SIGNALING", "REQUEUE_HOLD", "REQUEUE_FED"}
SUCCEEDED = "COMPLETED"
# sacct can lag a fresh submission by a few seconds; after this, a job it
# doesn't know about has vanished.
VANISHED_AFTER = timedelta(minutes=10)
SACCT_FIELDS = "JobID,State,ExitCode,Elapsed,CPUTimeRAW,NodeList"
RESERVED = {"code", "in", "out"}
TREE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SSH = ["ssh", "-o", "BatchMode=yes"]


@dataclass
class Host:
    name: str
    ssh: str
    workdir: str
    slots: int
    partition: str | None = None
    account: str | None = None
    time: str = "1:00:00"
    mem: str = "4G"
    cpus: int = 1
    env: dict[str, str] = field(default_factory=dict)
    fetch_max_mb: int = 500
    fetch_exclude: list[str] = field(default_factory=list)


def host(program: Program, name: str) -> Host:
    hosts = program.hosts
    if name not in hosts:
        allowed = ", ".join(sorted(hosts)) or "none"
        raise SystemExit(f"host {name!r} is not in program.toml [hosts] (allowed: {allowed}); "
                         f"using a new host is {program.scientist}'s decision")
    spec = dict(hosts[name])
    if spec.pop("kind", None) != "slurm":
        raise SystemExit(f"[hosts.{name}] kind must be \"slurm\" (the only kind built)")
    for key in ("ssh", "workdir", "slots"):
        if key not in spec:
            raise SystemExit(f"[hosts.{name}] is missing {key}")
    if not spec["workdir"].startswith("/"):
        raise SystemExit(f"[hosts.{name}] workdir must be an absolute path on the cluster")
    try:
        return Host(name=name, **spec)
    except TypeError as error:
        raise SystemExit(f"[hosts.{name}]: {error}") from None


# --- talking to the cluster ------------------------------------------------

def remote(h: Host, command: str, stdin: bytes | None = None) -> str:
    done = subprocess.run([*SSH, h.ssh, command], input=stdin, capture_output=True)
    if done.returncode != 0:
        raise RuntimeError(f"on {h.name}: `{command}` failed ({done.returncode}): "
                           f"{done.stderr.decode().strip() or done.stdout.decode().strip()}")
    return done.stdout.decode()


def rsync(args: list[str], what: str) -> None:
    done = subprocess.run(["rsync", "-a", "-e", shlex.join(SSH), *args],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"{what} failed: {done.stderr.strip()}")


# --- code trees --------------------------------------------------------------

@dataclass
class Tree:
    name: str          # folder in the run folder
    repo: Path         # local git repository
    commit: str
    branch: str | None

    def record(self) -> dict:
        return {"repo": str(self.repo), "commit": self.commit, "branch": self.branch}


def resolve(repo: Path, name: str, rev: str) -> Tree:
    commit = git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    full = git(repo, "rev-parse", "--symbolic-full-name", rev, check=False).strip()
    branch = full.removeprefix("refs/heads/") if full.startswith("refs/heads/") else None
    return Tree(name, repo, commit, branch)


def clean(repo: Path, why: str) -> None:
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no").strip()
    if dirty:
        raise SystemExit(f"{repo} has uncommitted changes to tracked files; commit first, "
                         f"since {why}:\n{dirty}")


def path_sources(program: Program, commit: str) -> list[Tree]:
    """The uv path sources outside the repository named by pyproject.toml at
    ``commit``, each at its repository's HEAD, placed beside ``code/`` the way
    the relative path expects."""
    text = show(program.repo, commit, "pyproject.toml")
    if text is None:
        return []
    sources = tomllib.loads(text.decode()).get("tool", {}).get("uv", {}).get("sources", {})
    trees = []
    for package, spec in sources.items():
        if "path" not in spec:
            continue
        local = (program.repo / spec["path"]).resolve()
        if local.is_relative_to(program.repo):
            continue  # inside the repository, so already in code/
        name = posixpath.normpath(posixpath.join("code", spec["path"]))
        if name.startswith("..") or "/" in name or name in RESERVED:
            raise SystemExit(f"uv source {package} at {spec['path']} would land at "
                             f"{name!r}; only a sibling of the repository is supported")
        top = git(local, "rev-parse", "--show-toplevel", check=False).strip()
        if Path(top).resolve() != local:
            raise SystemExit(f"uv source {package} at {local} is not the top of a git "
                             "repository, so it can't be shipped at a known commit")
        clean(local, f"the job uses {package} from its committed code")
        trees.append(resolve(local, name, "HEAD"))
    return trees


def ship(h: Host, tree: Tree, run_dir: str) -> None:
    """Push the tree's commit to a bare repository on the host and check it out
    into the run folder."""
    q = shlex.quote
    bare = f"{h.workdir}/git/{tree.repo.name}.git"
    remote(h, f"mkdir -p {q(h.workdir + '/git')} && git init -q --bare {q(bare)}")
    done = subprocess.run(["git", "-C", str(tree.repo), "push", "-q", f"{h.ssh}:{bare}",
                           f"{tree.commit}:refs/roborobo/{tree.commit}"],
                          capture_output=True, text=True,
                          env={**os.environ, "GIT_SSH_COMMAND": shlex.join(SSH)})
    if done.returncode != 0:
        raise RuntimeError(f"pushing {tree.repo.name} {tree.commit[:10]} to {h.name} failed: "
                           f"{done.stderr.strip()}")
    dest = f"{run_dir}/{tree.name}"
    switch = f"-B {q(tree.branch)}" if tree.branch else "--detach"
    remote(h, f"git clone -q --shared --no-checkout {q(bare)} {q(dest)} && "
              f"git -C {q(dest)} checkout -q {switch} {tree.commit}")


def wrapper(program: Program, h: Host, experiment: str, run_dir: str, command: list[str],
            time: str, mem: str, cpus: int) -> str:
    """The sbatch script for one job."""
    q = shlex.quote
    options = [f"--job-name={program.name}-{experiment}",
               f"--output={run_dir}/slurm-%j.out",
               f"--time={time}", f"--mem={mem}", f"--cpus-per-task={cpus}", "--ntasks=1"]
    if h.partition:
        options.append(f"--partition={h.partition}")
    if h.account:
        options.append(f"--account={h.account}")
    env = {**h.env, "ROBOROBO_PROGRAM": program.name, "ROBOROBO_EXPERIMENT": experiment,
           "ROBOROBO_RUN": run_dir, "ROBOROBO_IN": f"{run_dir}/in",
           "ROBOROBO_OUT": f"{run_dir}/out"}
    return "\n".join([
        "#!/bin/bash -l",
        *(f"#SBATCH {option}" for option in options),
        "set -euo pipefail",
        *(f"export {key}={q(str(value))}" for key, value in env.items()),
        f"mkdir -p {q(run_dir + '/out')}",
        f"cd {q(run_dir + '/code')}",
        shlex.join(command),
        "",
    ])


def parse_sacct(text: str) -> dict[str, dict]:
    """``sacct -X -n -P --format=SACCT_FIELDS`` output, by job id."""
    rows = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        job_id, state, exit_code, elapsed, cpu_raw, nodes = line.split("|")
        rows[job_id] = {"state": state.split()[0],  # "CANCELLED by 1234" -> CANCELLED
                        "exit_code": exit_code, "elapsed": elapsed,
                        "cpu_hours": round(int(cpu_raw or 0) / 3600, 3), "nodes": nodes}
    return rows


# --- job records -------------------------------------------------------------

def data_dir(program: Program) -> Path:
    return Path(program.data_dir).expanduser()


def jobs_dir(program: Program) -> Path:
    return data_dir(program) / "jobs"


def save(program: Program, job: dict) -> None:
    path = jobs_dir(program) / f"{job['host']}-{job['job_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, indent=2) + "\n")


def load_jobs(program: Program) -> list[dict]:
    folder = jobs_dir(program)
    if not folder.exists():
        return []
    return sorted((json.loads(p.read_text()) for p in folder.glob("*.json")),
                  key=lambda job: job["submitted_at"])


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


# --- submit ------------------------------------------------------------------

def registration(program: Program, experiment: str) -> dict:
    """The experiment's live registration; fails if there is none or it has an outcome."""
    path = program.repo / "ledger" / "experiments.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    mine = [r for r in records if r.get("experiment_id") == experiment]
    registrations = [r for r in mine if r.get("record") == "registration"]
    if not registrations:
        raise SystemExit(f"{experiment} has no registration in ledger/experiments.jsonl; "
                         "register it (where it runs and why) before submitting")
    if any(r.get("record") == "outcome" for r in mine):
        raise SystemExit(f"{experiment} already has an outcome; register a new experiment")
    return registrations[-1]


def parse_trees(program: Program, specs: list[str]) -> list[Tree]:
    """``NAME=REV`` checks out REV of the program's repository; ``NAME=PATH:REV``
    checks out REV of another git repository, which must be in the data
    folder (a colon can't appear in a git revision, so the two can't be
    confused)."""
    trees = []
    root = data_dir(program).resolve()
    for spec in specs:
        name, sep, rest = spec.partition("=")
        if not sep or not TREE_NAME.fullmatch(name) or not rest:
            raise SystemExit(f"--tree {spec!r}: give NAME=REV (e.g. engine=method/nudge) or "
                             "NAME=PATH:REV for another repository in the data folder")
        if ":" not in rest:
            trees.append(resolve(program.repo, name, rest))
            continue
        path, _, rev = rest.rpartition(":")
        repo = Path(path).expanduser().resolve()
        if not repo.is_relative_to(root):
            raise SystemExit(f"--tree {spec!r}: {repo} is not in the program's data folder "
                             f"{root}; clone it there first")
        top = git(repo, "rev-parse", "--show-toplevel", check=False).strip()
        if not top or Path(top).resolve() != repo:
            raise SystemExit(f"--tree {spec!r}: {repo} is not the top of a git repository")
        rev = rev or "HEAD"
        if rev == "HEAD":
            clean(repo, f"the job runs {repo.name}'s committed code")
        trees.append(resolve(repo, name, rev))
    return trees


def check_inputs(program: Program, inputs: list[Path]) -> list[Path]:
    root = data_dir(program).resolve()
    checked = []
    for path in inputs:
        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"--input {path} does not exist")
        if not path.is_relative_to(root):
            raise SystemExit(f"--input {path} is not under the program's data folder {root}")
        checked.append(path)
    names = [p.name for p in checked]
    if len(set(names)) != len(names):
        raise SystemExit(f"--input names must differ; they land side by side in in/: {names}")
    return checked


def submit(program: Program, host_name: str, experiment: str, command: list[str],
           rev: str = "HEAD", time: str | None = None, mem: str | None = None,
           cpus: int | None = None, trees: list[str] = (),
           inputs: list[Path] = ()) -> dict:
    h = host(program, host_name)
    if not command:
        raise SystemExit("nothing to run: give the command after --")
    reg = registration(program, experiment)
    if reg.get("host", h.name) != h.name:
        raise SystemExit(f"{experiment} is registered to run on {reg['host']}, not {h.name}; "
                         "register a new experiment that supersedes it to move it")
    if rev == "HEAD":
        clean(program.repo, "the job runs the committed code")
    code = resolve(program.repo, "code", rev)
    all_trees = [code, *parse_trees(program, trees), *path_sources(program, code.commit)]
    names = [t.name for t in all_trees]
    clash = {n for n in names if names.count(n) > 1} | (set(names[1:]) & RESERVED)
    if clash:
        raise SystemExit(f"tree names clash or are reserved: {', '.join(sorted(clash))}")
    sent = check_inputs(program, list(inputs))
    busy = [j for j in load_jobs(program) if j["host"] == h.name and not j.get("ended_at")]
    if len(busy) >= h.slots:
        raise SystemExit(f"{h.name} has all {h.slots} slots in use "
                         f"({', '.join(j['job_id'] for j in busy)}); run `program jobs`")

    submitted = now()
    run_dir = f"{h.workdir}/{program.name}/{experiment}/{submitted:%Y%m%dT%H%M%SZ}"
    for tree in all_trees:
        ship(h, tree, run_dir)
    remote(h, f"mkdir -p {shlex.quote(run_dir + '/in')}")
    for path in sent:
        rsync([str(path), f"{h.ssh}:{run_dir}/in/"], f"sending {path} to {h.name}")
    sbatch = {"time": time or h.time, "mem": mem or h.mem, "cpus": cpus or h.cpus}
    script = wrapper(program, h, experiment, run_dir, command, **sbatch)
    q = shlex.quote(f"{run_dir}/job.sh")
    out = remote(h, f"cat > {q} && sbatch --parsable {q}", stdin=script.encode())
    job_id = out.strip().split(";")[0]
    if not job_id.isdigit():
        raise RuntimeError(f"sbatch on {h.name} gave no job id: {out!r}")
    job = {"host": h.name, "job_id": job_id, "experiment_id": experiment,
           "line_id": reg.get("line_id"), "commit": code.commit,
           "trees": {t.name: t.record() for t in all_trees},
           "inputs": [str(p) for p in sent], "command": command, "sbatch": sbatch,
           "run_dir": run_dir, "submitted_at": stamp(submitted), "state": "PENDING"}
    save(program, job)
    return job


# --- watch and collect -------------------------------------------------------

SIZE_SCRIPT = """\
import fnmatch, os, sys
out, log, *patterns = sys.argv[1:]
total = None
if os.path.isdir(out):
    total = 0
    for folder, _, files in os.walk(out):
        total += sum(os.path.getsize(os.path.join(folder, f)) for f in files
                     if not any(fnmatch.fnmatch(f, p) for p in patterns))
print("none" if total is None else total // 2**20, "log" if os.path.isfile(log) else "nolog")
"""


def outputs_dir(program: Program, job: dict) -> Path:
    return data_dir(program) / "runs" / job["experiment_id"] / f"{job['host']}-{job['job_id']}"


def sync(program: Program, h: Host, job: dict) -> str | None:
    """Copy the job's outputs (less fetch_exclude) and log into the data folder.
    Returns a problem if the outputs are too big to fetch."""
    dest = outputs_dir(program, job)
    dest.mkdir(parents=True, exist_ok=True)
    out, log = f"{job['run_dir']}/out", f"{job['run_dir']}/slurm-{job['job_id']}.out"
    args = shlex.join([out, log, *h.fetch_exclude])
    size, has_log = remote(h, f"python3 - {args}", stdin=SIZE_SCRIPT.encode()).split()
    if has_log == "log":
        rsync([f"{h.ssh}:{log}", f"{dest}/"], f"fetching {log} from {h.name}")
    if size == "none":
        return None
    if int(size) > h.fetch_max_mb:
        return (f"{job['experiment_id']} job {job['job_id']} on {h.name}: outputs are {size} MB "
                f"after fetch_exclude, over fetch_max_mb={h.fetch_max_mb}; fetched only the "
                f"log. They are in {out}")
    rsync([*(f"--exclude={p}" for p in h.fetch_exclude), f"{h.ssh}:{out}/", f"{dest}/out/"],
          f"fetching {out} from {h.name}")
    return None


@dataclass
class Report:
    active: list[dict] = field(default_factory=list)
    ended: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def refresh(program: Program) -> Report:
    """Ask each host about its unfinished jobs, sync their outputs, and record
    the ones that ended."""
    report = Report()
    open_jobs = [j for j in load_jobs(program) if not j.get("ended_at")]
    for host_name in sorted({j["host"] for j in open_jobs}):
        h = host(program, host_name)
        mine = [j for j in open_jobs if j["host"] == host_name]
        ids = ",".join(j["job_id"] for j in mine)
        rows = parse_sacct(remote(h, f"sacct -X -n -P -j {ids} --format={SACCT_FIELDS}"))
        for job in mine:
            row = rows.get(job["job_id"])
            if row is None:
                submitted = datetime.fromisoformat(job["submitted_at"].replace("Z", "+00:00"))
                if now() - submitted > VANISHED_AFTER:
                    report.problems.append(f"{job['experiment_id']} job {job['job_id']} on "
                                           f"{host_name} is unknown to sacct; it has vanished")
                report.active.append(job)
                continue
            job.update(row)
            problem = None if row["state"] == "PENDING" else sync(program, h, job)
            job["outputs"] = str(outputs_dir(program, job))
            if problem:
                report.problems.append(problem)
            if row["state"] in ACTIVE:
                job["synced_at"] = stamp(now())
                save(program, job)
                report.active.append(job)
                continue
            job["ended_at"] = stamp(now())
            record = Path("evidence") / job["experiment_id"] / f"{host_name}-{job['job_id']}.json"
            job["record"] = str(record)
            text = json.dumps(job, indent=2) + "\n"
            (program.repo / record).parent.mkdir(parents=True, exist_ok=True)
            (program.repo / record).write_text(text)
            (outputs_dir(program, job) / "job.json").write_text(text)
            save(program, job)
            report.ended.append(job)
            if row["state"] != SUCCEEDED:
                report.problems.append(f"{job['experiment_id']} job {job['job_id']} on "
                                       f"{host_name} ended {row['state']} (exit "
                                       f"{row['exit_code']}); log in {job['outputs']}")
    return report


def describe(report: Report) -> str:
    lines = []
    for job in report.ended:
        lines.append(f"ended   {job['host']} {job['job_id']} {job['experiment_id']} "
                     f"{job['state']} {job['elapsed']} {job['cpu_hours']} CPU-h "
                     f"-> {job['outputs']} (record {job['record']})")
    for job in report.active:
        where = f" -> {job['outputs']}" if job.get("synced_at") else ""
        lines.append(f"running {job['host']} {job['job_id']} {job['experiment_id']} "
                     f"{job['state']} {job.get('elapsed', '')}{where}")
    if not lines:
        lines.append("no jobs in flight")
    if report.problems:
        lines.append(f"\n{len(report.problems)} problem(s):")
        lines += [f"  - {p}" for p in report.problems]
    return "\n".join(lines) + "\n"
