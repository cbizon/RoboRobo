"""Jobs on Slurm clusters (Hatteras): ``program submit`` and ``program jobs``.

A job runs one command under ``sbatch`` on the program's code at one commit.
The code goes over with ``git archive``, so the cluster needs no access to
GitHub, and the commit is on record. The job writes its outputs to
``$ROBOROBO_OUT``.

Hosts are the scientist's: ``[hosts.<name>]`` in ``program.toml``, which is
human-only and approved, so the agent can't add a host or raise its limits.

A job's state lives outside git, in ``<data_dir>/jobs/<host>-<jobid>.json``.
When the job ends, ``program jobs`` copies its outputs, its Slurm log and a
``job.json`` into ``evidence/<experiment>/<host>-<jobid>/``. Every failure
(a job that failed, timed out, ran out of memory or vanished; outputs too
big to fetch) is reported and makes the command exit non-zero. Nothing is
retried on its own.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from roborobo.config import Program
from roborobo.gitutil import git

ACTIVE = {"PENDING", "RUNNING", "REQUEUED", "RESIZING", "SUSPENDED", "CONFIGURING",
          "COMPLETING", "STAGE_OUT", "SIGNALING", "REQUEUE_HOLD", "REQUEUE_FED"}
SUCCEEDED = "COMPLETED"
# sacct can lag a fresh submission by a few seconds; after this, a job it
# doesn't know about has vanished.
VANISHED_AFTER = timedelta(minutes=10)
SACCT_FIELDS = "JobID,State,ExitCode,Elapsed,CPUTimeRAW,NodeList"


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

def ssh_command(h: Host, command: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", h.ssh, command]


def remote(h: Host, command: str, stdin: bytes | None = None) -> str:
    done = subprocess.run(ssh_command(h, command), input=stdin, capture_output=True)
    if done.returncode != 0:
        raise RuntimeError(f"on {h.name}: `{command}` failed ({done.returncode}): "
                           f"{done.stderr.decode().strip() or done.stdout.decode().strip()}")
    return done.stdout.decode()


def ship_code(program: Program, h: Host, commit: str, code_dir: str) -> None:
    """Unpack the program's tree at ``commit`` into ``code_dir`` on the host."""
    archive = subprocess.Popen(["git", "-C", str(program.repo), "archive", "--format=tar", commit],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    q = shlex.quote(code_dir)
    unpack = subprocess.run(ssh_command(h, f"mkdir -p {q} && tar -x -C {q}"),
                            stdin=archive.stdout, capture_output=True)
    archive.stdout.close()
    archive_err = archive.stderr.read().decode().strip()
    if archive.wait() != 0:
        raise RuntimeError(f"git archive {commit} failed: {archive_err}")
    if unpack.returncode != 0:
        raise RuntimeError(f"unpacking the code on {h.name} failed: "
                           f"{unpack.stderr.decode().strip()}")


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

def jobs_dir(program: Program) -> Path:
    return Path(program.data_dir).expanduser() / "jobs"


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


def submit(program: Program, host_name: str, experiment: str, command: list[str],
           rev: str = "HEAD", time: str | None = None, mem: str | None = None,
           cpus: int | None = None) -> dict:
    h = host(program, host_name)
    if not command:
        raise SystemExit("nothing to run: give the command after --")
    reg = registration(program, experiment)
    if rev == "HEAD":
        dirty = git(program.repo, "status", "--porcelain", "--untracked-files=no").strip()
        if dirty:
            raise SystemExit("tracked files have uncommitted changes; commit first, since "
                             f"the job runs the committed code:\n{dirty}")
    commit = git(program.repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    busy = [j for j in load_jobs(program) if j["host"] == h.name and not j.get("ended_at")]
    if len(busy) >= h.slots:
        raise SystemExit(f"{h.name} has all {h.slots} slots in use "
                         f"({', '.join(j['job_id'] for j in busy)}); run `program jobs`")

    submitted = now()
    run_dir = f"{h.workdir}/{program.name}/{experiment}/{submitted:%Y%m%dT%H%M%SZ}"
    ship_code(program, h, commit, f"{run_dir}/code")
    sbatch = {"time": time or h.time, "mem": mem or h.mem, "cpus": cpus or h.cpus}
    script = wrapper(program, h, experiment, run_dir, command, **sbatch)
    q = shlex.quote(f"{run_dir}/job.sh")
    out = remote(h, f"cat > {q} && sbatch --parsable {q}", stdin=script.encode())
    job_id = out.strip().split(";")[0]
    if not job_id.isdigit():
        raise RuntimeError(f"sbatch on {h.name} gave no job id: {out!r}")
    job = {"host": h.name, "job_id": job_id, "experiment_id": experiment,
           "line_id": reg.get("line_id"), "commit": commit, "command": command,
           "sbatch": sbatch, "run_dir": run_dir,
           "submitted_at": submitted.isoformat().replace("+00:00", "Z"),
           "state": "PENDING"}
    save(program, job)
    return job


# --- watch and collect -------------------------------------------------------

def fetch(program: Program, h: Host, job: dict) -> tuple[Path, str | None]:
    """Copy the job's outputs and log into evidence/. Returns the folder and a
    problem, if the outputs were too big to fetch."""
    dest = program.repo / "evidence" / job["experiment_id"] / f"{h.name}-{job['job_id']}"
    dest.mkdir(parents=True, exist_ok=True)
    run_dir = job["run_dir"]
    out, log = f"{run_dir}/out", f"{run_dir}/slurm-{job['job_id']}.out"
    q = shlex.quote
    # A job cancelled while pending has neither; one that failed early may lack out/.
    size, has_log = remote(h, f"if [ -d {q(out)} ]; then du -sm {q(out)} | cut -f1; "
                              f"else echo none; fi; "
                              f"if [ -f {q(log)} ]; then echo log; else echo nolog; fi").split()
    problem = None
    sources = [log] if has_log == "log" else []
    if size != "none" and int(size) > h.fetch_max_mb:
        problem = (f"{job['experiment_id']} job {job['job_id']} on {h.name}: outputs are "
                   f"{size} MB, over fetch_max_mb={h.fetch_max_mb}; fetched only the log. "
                   f"They are in {out}")
    elif size != "none":
        sources.append(f"{out}/")
    for source in sources:
        target = dest / "out" if source.endswith("/out/") else dest
        target.mkdir(exist_ok=True)
        done = subprocess.run(["rsync", "-a", "-e", "ssh -o BatchMode=yes",
                               f"{h.ssh}:{source}", f"{target}/"], capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError(f"fetching {source} from {h.name} failed: {done.stderr.strip()}")
    return dest, problem


@dataclass
class Report:
    active: list[dict] = field(default_factory=list)
    ended: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def refresh(program: Program) -> Report:
    """Ask each host about its unfinished jobs; collect the ones that ended."""
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
            if row["state"] in ACTIVE:
                save(program, job)
                report.active.append(job)
                continue
            dest, problem = fetch(program, h, job)
            job["ended_at"] = now().isoformat().replace("+00:00", "Z")
            job["evidence"] = str(dest.relative_to(program.repo))
            (dest / "job.json").write_text(json.dumps(job, indent=2) + "\n")
            save(program, job)
            report.ended.append(job)
            if problem:
                report.problems.append(problem)
            if row["state"] != SUCCEEDED:
                report.problems.append(f"{job['experiment_id']} job {job['job_id']} on "
                                       f"{host_name} ended {row['state']} (exit "
                                       f"{row['exit_code']}); log in {job['evidence']}")
    return report


def describe(report: Report) -> str:
    lines = []
    for job in report.ended:
        lines.append(f"ended   {job['host']} {job['job_id']} {job['experiment_id']} "
                     f"{job['state']} {job['elapsed']} {job['cpu_hours']} CPU-h "
                     f"-> {job['evidence']}")
    for job in report.active:
        lines.append(f"running {job['host']} {job['job_id']} {job['experiment_id']} "
                     f"{job['state']} {job.get('elapsed', '')}".rstrip())
    if not lines:
        lines.append("no jobs in flight")
    if report.problems:
        lines.append(f"\n{len(report.problems)} problem(s):")
        lines += [f"  - {p}" for p in report.problems]
    return "\n".join(lines) + "\n"
