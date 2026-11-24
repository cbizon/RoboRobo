"""Artifacts: recording data here and on a cluster, the coverage check,
deleting, fetching to look at, and inputs from earlier jobs. Uses the
stand-in cluster from test_cluster."""

import json
import subprocess

from test_cluster import git, isolated_git, program, run, slurm, tools  # noqa: F401

from roborobo import artifacts, config, night
from roborobo.records import load_ledger


def data(program):
    return program.parent / "demo-data"


def artifact_check(program):
    prog = config.load(program)
    return artifacts.check(prog, load_ledger(prog)[0]["artifacts.jsonl"])


def records(program):
    text = (program / "ledger" / "artifacts.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


def make(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def finished_job(program, *command):
    assert run("submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001",
               "--", *(command or ("bash", "sim.sh", "7"))) == 0
    assert run("jobs", "--repo", program) == 0


def test_local_data_must_be_recorded(program, capsys):
    make(data(program) / "runs" / "E0001" / "a.txt")
    problems, _ = artifact_check(program)
    assert problems == ["the data folder has unrecorded data: runs/E0001/. Record data with "
                        "`program record <folder>`; scratch goes in tmp/"]
    assert run("record", "--repo", program, data(program) / "runs" / "E0001") == 0
    assert "created A0001 for runs/E0001" in capsys.readouterr().out
    assert artifact_check(program) == ([], [])
    (record,) = records(program)
    assert record["experiment_id"] == "E0001" and record["line_id"] == "L0001"
    assert record["provenance"] == "recorded" and record["files"] == 1
    assert (program / "ledger/manifests/A0001-1.tsv").read_text().endswith("\t2\ta.txt\n")


def test_a_changed_artifact_is_noted_then_rerecorded(program, capsys):
    make(data(program) / "runs" / "E0001" / "a.txt")
    run("record", "--repo", program, "runs/E0001")
    make(data(program) / "runs" / "E0001" / "b.txt", "more\n")
    problems, notes = artifact_check(program)
    assert problems == [] and "A0001 (runs/E0001) changed since it was recorded" in notes[0]
    assert run("record", "--repo", program, "runs/E0001") == 0
    assert "updated A0001" in capsys.readouterr().out
    assert records(program)[-1]["manifest"] == "ledger/manifests/A0001-2.tsv"
    assert artifact_check(program) == ([], [])


def test_scratch_code_and_operational_state_need_no_record(program):
    for rel in ("tmp/x", "logs/x", "jobs/x.json", "mirror/E0001/x.json", "map/index.html",
                "runs/active.jsonl", ".DS_Store"):
        make(data(program) / rel)
    clone = data(program) / "worktrees" / "nudge"
    clone.mkdir(parents=True)
    git(clone, "init", "-q")
    make(clone / "code.py")
    (data(program) / "worktrees" / "linked").symlink_to(clone)  # a link to code is code
    assert artifact_check(program) == ([], [])


def test_vanished_local_data_is_a_problem(program):
    make(data(program) / "runs" / "E0001" / "a.txt")
    run("record", "--repo", program, "runs/E0001")
    (data(program) / "runs" / "E0001" / "a.txt").unlink()
    (data(program) / "runs" / "E0001").rmdir()
    problems, _ = artifact_check(program)
    assert "A0001 (runs/E0001) is gone from the data folder but has no deleted record" in problems[0]


def test_a_tampered_manifest_is_a_problem(program):
    make(data(program) / "runs" / "E0001" / "a.txt")
    run("record", "--repo", program, "runs/E0001")
    (program / "ledger/manifests/A0001-1.tsv").write_text("forged\n")
    assert "doesn't match its recorded sha256" in artifact_check(program)[0][0]


def test_records_refuse_scratch_code_and_overlaps(program):
    make(data(program) / "tmp" / "x")
    assert "holds scratch, not data" in run("record", "--repo", program, "tmp")
    clone = data(program) / "other"
    clone.mkdir()
    git(clone, "init", "-q")
    assert "is in a git repository" in run("record", "--repo", program, "other")
    make(data(program) / "runs" / "E0001" / "inner" / "a.txt")
    run("record", "--repo", program, "runs/E0001/inner")
    assert "overlaps A0001" in run("record", "--repo", program, "runs/E0001")


def test_backfill_records_everything_unrecorded(program, capsys):
    make(data(program) / "bulk-pilot" / "a.txt")
    make(data(program) / "runs" / "E0001" / "b.txt")
    make(data(program) / "tmp" / "c.txt")
    assert run("record", "--repo", program, "--backfill", "--by", "human") == 0
    assert capsys.readouterr().out.split() == ["A0001", "bulk-pilot/", "A0002", "runs/E0001/"]
    first, second = records(program)
    assert first["provenance"] == "backfilled" and first["by"] == "human"
    assert "experiment_id" not in first and second["experiment_id"] == "E0001"
    assert artifact_check(program) == ([], [])


def test_delete_local_data(program, capsys):
    make(data(program) / "runs" / "E0001" / "a.txt")
    run("record", "--repo", program, "runs/E0001")
    assert run("delete", "--repo", program, "A0001", "--reason", "a pilot nobody will use") == 0
    assert not (data(program) / "runs" / "E0001").exists()
    assert records(program)[-1]["record"] == "deleted"
    assert artifact_check(program) == ([], [])
    assert "A0001 is not a live artifact" in run(
        "delete", "--repo", program, "A0001", "--reason", "twice is too many")


def test_cited_data_cannot_be_deleted(program):
    make(data(program) / "runs" / "E0001" / "a.txt")
    run("record", "--repo", program, "runs/E0001")
    with (program / "ledger" / "experiments.jsonl").open("a") as ledger:
        ledger.write(json.dumps({"record": "outcome", "experiment_id": "E0001",
                                 "evidence": ["A0001"]}) + "\n")
    assert "A0001 is cited in ledger/experiments.jsonl" in run(
        "delete", "--repo", program, "A0001", "--reason", "should not be allowed")
    assert (data(program) / "runs" / "E0001" / "a.txt").exists()


def test_delete_cluster_data(program):
    finished_job(program)
    run_dir = records(program)[0]["path"]
    assert subprocess.run(["test", "-d", run_dir]).returncode == 0
    assert "deleting data on a cluster is Pat's to do" in run(
        "delete", "--repo", program, "A0001", "--reason", "superseded by a rerun")
    assert run("delete", "--repo", program, "A0001", "--by", "human", "--reason",
               "superseded by a rerun") == 0
    assert subprocess.run(["test", "-d", run_dir]).returncode != 0
    deleted = records(program)[-1]
    assert (deleted["record"], deleted["artifact_id"], deleted["reason"]) == \
        ("deleted", "A0001", "superseded by a rerun")


def test_fetch_to_look_and_the_runner_empties_it(program, capsys):
    finished_job(program)
    capsys.readouterr()
    assert run("fetch", "--repo", program, "A0001:out/checkpoint-0001.npz") == 0
    fetched = data(program) / "tmp" / "fetch" / "A0001" / "out" / "checkpoint-0001.npz"
    assert capsys.readouterr().out.strip() == str(fetched)
    assert fetched.read_text() == "big\n"
    assert artifact_check(program) == ([], [])  # scratch, not data
    night.empty_fetched(config.load(program))
    assert not (data(program) / "tmp" / "fetch").exists()


def test_fetch_refuses_too_much(program):
    finished_job(program)
    assert "over the limit of 0 MB" in run("fetch", "--repo", program, "A0001", "--max-mb", "0")
    assert "doesn't exist on hatteras" in run("fetch", "--repo", program, "A0001:out/nothing")


def test_input_from_an_earlier_job_on_the_cluster(program, capsys):
    finished_job(program)
    assert run("submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001",
               "--input", "A0001:out", "--", "bash", "-c",
               'cat "$ROBOROBO_IN/out/result.txt"; echo changed > "$ROBOROBO_IN/out/result.txt"'
               ) == 0
    assert run("jobs", "--repo", program) == 0
    log = (data(program) / "mirror/E0001/hatteras-1002/slurm-1002.out").read_text()
    assert log == "seed 7\n"
    first, second = records(program)
    assert second["inputs"] == [{"artifact_id": "A0001", "path": "out"}]
    with open(first["path"] + "/out/result.txt") as original:  # the job changed only its copy
        assert original.read() == "seed 7\n"


def test_unrecorded_inputs_are_refused(program):
    make(data(program) / "runs" / "E0000" / "start" / "checkpoint.npz")
    assert "is not in a recorded artifact; record it first" in run(
        "submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001",
        "--input", data(program) / "runs" / "E0000" / "start", "--", "true")


def test_uncommitted_records_dont_block_submitting(program):
    make(program / "evidence" / "E0001" / "note.json", "{}\n")
    git(program, "add", "-A")
    git(program, "commit", "-q", "-m", "evidence")
    make(program / "evidence" / "E0001" / "note.json", '{"changed": true}\n')
    assert run("submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001",
               "--", "true") == 0


def test_fetch_exclude_is_replaced_by_fetch(program):
    toml = program / "program.toml"
    toml.write_text(toml.read_text().replace('mem = "1G"', 'mem = "1G"\nfetch_exclude = ["*.npz"]'))
    git(program, "commit", "-q", "-am", "old setting")
    assert "fetch_exclude is replaced by fetch" in run(
        "submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001", "--", "true")


def test_jobs_that_ended_before_artifacts_get_one(program, capsys):
    finished_job(program)
    prog = config.load(program)
    from roborobo import cluster
    job = cluster.load_jobs(prog)[0]
    del job["artifact_id"]
    cluster.save(prog, job)
    (program / "ledger" / "artifacts.jsonl").write_text("")
    assert run("jobs", "--repo", program) == 0
    assert records(program)[0]["job"]["job_id"] == "1001"
    assert cluster.load_jobs(prog)[0]["artifact_id"] == "A0001"


def test_move_local_data_to_the_cluster(program, capsys):
    make(data(program) / "runs" / "E0001" / "a.txt", "first\n")
    make(data(program) / "runs" / "E0001" / "sub" / "b.txt", "second\n")
    run("record", "--repo", program, "runs/E0001")
    capsys.readouterr()
    assert run("move", "--repo", program, "A0001", "--host", "hatteras", "--by", "human") == 0
    assert "verified file by file" in capsys.readouterr().out
    moved = records(program)[-1]
    assert (moved["record"], moved["host"], moved["by"]) == ("moved", "hatteras", "human")
    assert moved["path"].endswith("/demo/data/A0001") and moved["files"] == 2
    with open(moved["path"] + "/sub/b.txt") as there:
        assert there.read() == "second\n"
    assert not (data(program) / "runs" / "E0001").exists()  # deleted here only after the check
    assert artifact_check(program) == ([], [])
    assert run("fetch", "--repo", program, "A0001:a.txt") == 0  # it is a cluster artifact now
    assert (data(program) / "tmp" / "fetch" / "A0001" / "a.txt").read_text() == "first\n"
    assert "A0001 is on hatteras, not on this machine" in run(
        "move", "--repo", program, "A0001", "--host", "hatteras")


def test_a_move_never_overwrites(program, tmp_path):
    make(data(program) / "runs" / "E0001" / "a.txt")
    run("record", "--repo", program, "runs/E0001")
    (tmp_path / "cluster" / "demo" / "data" / "A0001").mkdir(parents=True)
    assert "already exists on hatteras; a move never overwrites" in run(
        "move", "--repo", program, "A0001", "--host", "hatteras")
    assert (data(program) / "runs" / "E0001" / "a.txt").exists()


def test_cancel_a_job_in_flight(program, slurm, tmp_path, capsys):  # noqa: F811
    scancel = tmp_path / "bin" / "scancel"
    scancel.write_text(f'#!/bin/sh\necho CANCELLED > "{slurm}/$1"\n')
    scancel.chmod(0o755)
    (slurm / "hold").touch()
    assert run("submit", "--repo", program, "--host", "hatteras", "--experiment", "E0001",
               "--", "true") == 0
    assert "isn't a job in flight" in run("cancel", "--repo", program, "9999", "--reason", "no such job")
    assert run("cancel", "--repo", program, "hatteras-1001", "--reason", "wrong box size") == 0
    capsys.readouterr()
    assert run("jobs", "--repo", program) == 0  # cancelled on request: not a problem
    out = capsys.readouterr().out
    assert "ended   hatteras 1001 E0001 CANCELLED" in out and "problem" not in out
    recorded = json.loads((program / "evidence" / "E0001" / "hatteras-1001.json").read_text())
    assert recorded["cancel_requested"]["reason"] == "wrong box size"
