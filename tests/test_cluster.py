"""program submit and program jobs, end to end against a stand-in cluster.

``ssh``, ``sbatch`` and ``sacct`` on PATH are small scripts: ssh runs its
command here (so git archive, tar and rsync really run), sbatch runs the job
script at once and records how it ended, and sacct reports that. Everything
but Slurm itself is real.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from roborobo import cli, cluster, config

FAKE_SSH = """\
import subprocess, sys
args = sys.argv[1:]
while args[0].startswith("-"):
    args = args[2:] if args[0] == "-o" else args[1:]
sys.exit(subprocess.run(["bash", "-c", " ".join(args[1:])]).returncode)
"""

FAKE_SBATCH = """\
import os, pathlib, re, subprocess, sys
state = pathlib.Path(os.environ["FAKE_SLURM"])
script = pathlib.Path(sys.argv[-1])
counter = state / "next"
job_id = int(counter.read_text()) if counter.exists() else 1001
counter.write_text(str(job_id + 1))
if (state / "hold").exists():
    (state / str(job_id)).write_text("PENDING")
else:
    log = re.search(r"#SBATCH --output=(\\S+)", script.read_text())[1].replace("%j", str(job_id))
    with open(log, "w") as out:
        code = subprocess.run(["bash", str(script)], stdout=out, stderr=subprocess.STDOUT).returncode
    (state / str(job_id)).write_text("COMPLETED" if code == 0 else f"FAILED {code}")
print(f"{job_id};hatteras")
"""

FAKE_SACCT = """\
import os, pathlib, sys
state = pathlib.Path(os.environ["FAKE_SLURM"])
ids = sys.argv[sys.argv.index("-j") + 1].split(",")
for job_id in ids:
    path = state / job_id
    if path.exists():
        status, *code = path.read_text().split()
        print(f"{job_id}|{status}|{code[0] if code else 0}:0|00:00:02|7200|c0101")
"""


def run(*argv):
    with pytest.raises(SystemExit) as done:
        cli.main([str(a) for a in argv])
    return done.value.code


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (tmp_path / "gitconfig").write_text(
        "[user]\n\tname = Test Scientist\n\temail = t@example.org\n"
        "[init]\n\tdefaultBranch = main\n")


@pytest.fixture
def slurm(monkeypatch, tmp_path):
    """A stand-in cluster; returns its state folder."""
    bin_dir, state = tmp_path / "bin", tmp_path / "slurm"
    bin_dir.mkdir()
    state.mkdir()
    for name, source in (("ssh", FAKE_SSH), ("sbatch", FAKE_SBATCH), ("sacct", FAKE_SACCT)):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SLURM", str(state))
    return state


REGISTRATION = {"record": "registration", "experiment_id": "E0001",
                "at": "2026-11-19T12:00:00Z", "by": "agent", "rq_id": "RQ1",
                "line_id": "L0001", "hypothesis": "h", "method": "on hatteras, because long",
                "judged_by": "j", "falsified_if": "f", "cost_estimate": {"compute_hours": 1}}


@pytest.fixture
def program(tmp_path, slurm):
    repo = tmp_path / "demo"
    assert run("new", repo, "--name", "demo", "--title", "A demonstration program",
               "--scientist", "Pat", "--data-dir", str(tmp_path / "demo-data")) == 0
    with (repo / "program.toml").open("a") as toml:
        toml.write(f'\n[hosts.hatteras]\nkind = "slurm"\nssh = "ht1"\n'
                   f'workdir = "{tmp_path / "cluster"}"\nslots = 2\nmem = "1G"\n'
                   f'env = {{UV_LINK_MODE = "copy"}}\n')
    (repo / "ledger" / "experiments.jsonl").write_text(json.dumps(REGISTRATION) + "\n")
    (repo / "sim.sh").write_text('echo "seed $1" > "$ROBOROBO_OUT/result.txt"\n'
                                 'echo "uv mode $UV_LINK_MODE"\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "register E0001")
    return repo


def test_new_program_documents_hosts(program):
    assert "# [hosts.hatteras]" in (program / "program.toml").read_text()


def test_submit_then_collect(program, slurm, capsys):
    assert run("submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001",
               "--", "bash", "sim.sh", "7") == 0
    assert "submitted E0001 as job 1001 on hatteras" in capsys.readouterr().out
    job = cluster.load_jobs(config.load(program))[0]
    assert job["commit"] == git(program, "rev-parse", "HEAD").strip()
    assert job["line_id"] == "L0001" and job["sbatch"]["mem"] == "1G"
    script = (Path(job["run_dir"]) / "job.sh").read_text()
    assert "#SBATCH --job-name=demo-E0001" in script and "#SBATCH --mem=1G" in script
    assert "export UV_LINK_MODE=copy" in script

    assert run("jobs", "--repo", program) == 0
    out = capsys.readouterr().out
    assert "ended   hatteras 1001 E0001 COMPLETED" in out and "2.0 CPU-h" in out
    evidence = program / "evidence" / "E0001" / "hatteras-1001"
    assert (evidence / "out" / "result.txt").read_text() == "seed 7\n"
    assert "uv mode copy" in (evidence / "slurm-1001.out").read_text()
    recorded = json.loads((evidence / "job.json").read_text())
    assert recorded["state"] == "COMPLETED" and recorded["cpu_hours"] == 2.0

    assert run("jobs", "--repo", program) == 0
    assert "no jobs in flight" in capsys.readouterr().out


def test_failed_job_is_reported_and_its_log_fetched(program, capsys):
    assert run("submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001",
               "--", "bash", "-c", "echo broke; exit 3") == 0
    assert run("jobs", "--repo", program) == 1
    out = capsys.readouterr().out
    assert "ended FAILED (exit 3:0)" in out
    assert "broke" in (program / "evidence/E0001/hatteras-1001/slurm-1001.out").read_text()


def test_pending_jobs_hold_slots(program, slurm, capsys):
    (slurm / "hold").touch()
    for _ in range(2):
        assert run("submit", "--repo", program, "--host", "hatteras", "--experiment",
                   "E0001", "--", "true") == 0
    assert "all 2 slots in use" in run("submit", "--repo", program, "--host", "hatteras",
                                       "--experiment", "E0001", "--", "true")
    capsys.readouterr()
    assert run("jobs", "--repo", program) == 0
    assert capsys.readouterr().out.count("running hatteras") == 2


def test_vanished_job_is_a_problem(program, slurm, capsys):
    (slurm / "hold").touch()
    run("submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001", "--", "true")
    (slurm / "1001").unlink()
    prog = config.load(program)
    job = cluster.load_jobs(prog)[0]
    job["submitted_at"] = "2026-01-01T00:00:00Z"
    cluster.save(prog, job)
    assert run("jobs", "--repo", program) == 1
    assert "unknown to sacct; it has vanished" in capsys.readouterr().out


def test_big_outputs_stay_on_the_cluster(program, capsys):
    toml = program / "program.toml"
    toml.write_text(toml.read_text().replace('mem = "1G"', 'mem = "1G"\nfetch_max_mb = 1'))
    git(program, "commit", "-q", "-am", "smaller fetch limit")
    run("submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001", "--",
        "bash", "-c", 'head -c 3000000 /dev/zero > "$ROBOROBO_OUT/big"')
    assert run("jobs", "--repo", program) == 1
    assert "over fetch_max_mb=1; fetched only the log" in capsys.readouterr().out
    assert not (program / "evidence/E0001/hatteras-1001/out").exists()


def test_unknown_host_is_refused(program):
    assert "host 'ashe' is not in program.toml" in run(
        "submit", "--repo", program, "--host", "ashe", "--experiment", "E0001", "--", "true")


def test_unregistered_experiment_is_refused(program):
    assert "E0002 has no registration" in run(
        "submit", "--repo", program, "--host", "hatteras", "--experiment", "E0002", "--", "true")


def test_experiment_with_outcome_is_refused(program):
    outcome = {"record": "outcome", "experiment_id": "E0001", "at": "2026-11-19T13:00:00Z",
               "by": "agent", "status": "supported", "result": "r", "cost_actual": {},
               "evidence": []}
    with (program / "ledger" / "experiments.jsonl").open("a") as ledger:
        ledger.write(json.dumps(outcome) + "\n")
    git(program, "commit", "-q", "-am", "outcome")
    assert "already has an outcome" in run(
        "submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001", "--", "true")


def test_uncommitted_code_is_refused(program):
    (program / "sim.sh").write_text("echo changed\n")
    assert "commit first" in run("submit", "--repo", program, "--host", "hatteras",
                                 "--experiment", "E0001", "--", "bash", "sim.sh")


def test_parse_sacct():
    rows = cluster.parse_sacct("12|CANCELLED by 501|0:15|00:01:00|60|\n"
                               "13|OUT_OF_MEMORY|0:125|01:00:00|14400|c01\n")
    assert rows["12"]["state"] == "CANCELLED" and rows["12"]["cpu_hours"] == 0.017
    assert rows["13"] == {"state": "OUT_OF_MEMORY", "exit_code": "0:125",
                          "elapsed": "01:00:00", "cpu_hours": 4.0, "nodes": "c01"}
