"""The program command, end to end, against a local bare repository as the
remote."""

import json
import subprocess
from pathlib import Path

import pytest

from roborobo import cli


def run(*argv):
    with pytest.raises(SystemExit) as done:
        cli.main([str(a) for a in argv])
    return done.value.code


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch, tmp_path):
    """No global git config (signing, hooks), a fixed identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (tmp_path / "gitconfig").write_text(
        "[user]\n\tname = Test Scientist\n\temail = t@example.org\n"
        "[init]\n\tdefaultBranch = main\n")


@pytest.fixture
def program(tmp_path):
    repo = tmp_path / "demo"
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    assert run("new", repo, "--name", "demo", "--title", "A demonstration program",
               "--scientist", "Pat", "--data-dir", str(tmp_path / "demo-data")) == 0
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    return repo


def commit(repo, message="change"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def check(repo, capsys):
    code = run("check", "--repo", repo)
    return code, capsys.readouterr().out


def test_new_renders_the_template(program):
    claude = (program / "CLAUDE.md").read_text()
    assert "Pat reads your summary" in claude
    assert "{{" not in claude
    session = (program / "scheduled" / "session.md").read_text()
    assert "scheduled for 23:00 (America/New_York)" in session
    assert "commit by 05:00" in session
    settings = json.loads((program / ".claude" / "settings.json").read_text())
    assert "Edit(DIRECTION.md)" in settings["permissions"]["deny"]
    assert "Bash(git push:*)" in settings["permissions"]["deny"]
    assert "Bash" in settings["permissions"]["allow"]
    assert settings["permissions"]["additionalDirectories"][0].endswith("demo-data")
    assert (program / "DIRECTION.md").read_text().startswith("# Direction: demo")
    assert "Pat's background" in (program / "notes" / "README.md").read_text()
    assert "Edit(notes/**)" in settings["permissions"]["deny"]


def test_check_needs_an_approval(program, capsys):
    code, out = check(program, capsys)
    assert code == 1 and "no approval on origin" in out


def test_approve_then_check_passes(program, capsys):
    assert run("approve", "--repo", program) == 0
    code, out = check(program, capsys)
    assert code == 0, out
    assert "latest approval: approved/" in out
    assert "approvals in the last day" in out
    assert git(program, "rev-parse", "HEAD") == git(program, "rev-parse", "origin/main")


def test_changed_human_file_fails(program, capsys):
    run("approve", "--repo", program)
    with (program / "DIRECTION.md").open("a") as f:
        f.write("\nThe agent widened its own scope.\n")
    commit(program)
    code, out = check(program, capsys)
    assert code == 1 and "DIRECTION.md changed since approval" in out


def test_new_human_file_fails_until_approved(program, capsys):
    run("approve", "--repo", program)
    (program / "referee" / "score.py").write_text("print('ok')\n")
    commit(program)
    code, out = check(program, capsys)
    assert code == 1 and "referee/score.py is human-only but not in approval" in out
    run("approve", "--repo", program)
    assert check(program, capsys)[0] == 0


def test_forged_local_approval_does_not_count(program, capsys):
    """An agent that edits a human file, rewrites approvals.json and tags
    locally, without pushing, still fails: only the remote's tags count."""
    run("approve", "--repo", program)
    with (program / "budgets.yaml").open("a") as f:
        f.write("  cpu_hours: {limit: 999999, period: week}\n")
    import roborobo.approvals as ap
    from roborobo import config
    prog = config.load(program)
    (program / "approvals.json").write_text(json.dumps({"files": ap.manifest(prog)}))
    commit(program, "forged")
    git(program, "tag", "approved/20990101T000000Z")
    code, out = check(program, capsys)
    assert code == 1 and "budgets.yaml changed since approval" in out


def test_template_drift_fails_and_sync_fixes(program, capsys):
    run("approve", "--repo", program)
    (program / "CLAUDE.md").write_text("Do whatever you like.\n")
    code, out = check(program, capsys)
    assert code == 1 and "CLAUDE.md differs from RoboRobo's template" in out
    assert run("sync", "--repo", program) == 0
    assert check(program, capsys)[0] == 0


def registration(eid, **extra):
    return {"record": "registration", "experiment_id": eid, "at": "2026-11-05T23:10:00+00:00",
            "by": "agent", "rq_id": "RQ1", "hypothesis": "The thing changes the outcome.",
            "method": "Run it both ways and compare.", "judged_by": "The referee's comparison.",
            "falsified_if": "No difference beyond scatter.", "cost_estimate": {"cpu_hours": 2},
            **extra}


def append(repo, filename, record):
    with (repo / "ledger" / filename).open("a") as f:
        f.write(json.dumps(record) + "\n")


def test_ledger_records_validate(program, capsys):
    run("approve", "--repo", program)
    append(program, "experiments.jsonl", registration("E0001"))
    assert check(program, capsys)[0] == 0
    append(program, "experiments.jsonl", {"record": "outcome", "experiment_id": "E0002",
                                          "at": "2026-11-06T01:00:00+00:00", "by": "agent",
                                          "status": "supported", "result": "It changed a lot.",
                                          "cost_actual": {"cpu_hours": 1}, "evidence": []})
    code, out = check(program, capsys)
    assert code == 1 and "E0002 has an outcome without an earlier registration" in out


def test_schema_errors_are_reported(program, capsys):
    run("approve", "--repo", program)
    bad = registration("E0001")
    del bad["falsified_if"]
    append(program, "experiments.jsonl", bad)
    code, out = check(program, capsys)
    assert code == 1 and "'falsified_if' is a required property" in out


def test_ledger_is_append_only(program, capsys):
    run("approve", "--repo", program)
    append(program, "experiments.jsonl", registration("E0001"))
    commit(program)
    path = program / "ledger" / "experiments.jsonl"
    path.write_text(json.dumps(registration("E0001", hypothesis="A better hypothesis, after the fact.")) + "\n")
    code, out = check(program, capsys)
    assert code == 1 and "rewrites earlier lines" in out


def test_budget_over_limit_fails(program, capsys):
    (program / "budgets.yaml").write_text("budgets:\n  cpu_hours: {limit: 1, period: program}\n")
    commit(program)
    run("approve", "--repo", program)
    append(program, "experiments.jsonl", registration("E0001"))
    append(program, "experiments.jsonl", {"record": "outcome", "experiment_id": "E0001",
                                          "at": "2026-11-06T01:00:00+00:00", "by": "agent",
                                          "status": "supported", "result": "It changed a lot.",
                                          "cost_actual": {"cpu_hours": 3}, "evidence": []})
    code, out = check(program, capsys)
    assert code == 1 and "cpu_hours is over budget: 3 of 1 per program" in out


def test_open_blockers_must_be_in_the_summary(program, capsys):
    run("approve", "--repo", program)
    append(program, "blockers.jsonl", {"blocker_id": "B0001", "at": "2026-11-05T23:30:00+00:00",
                                       "by": "agent", "status": "open", "kind": "compute",
                                       "description": "The disk filled up mid-run."})
    summary = {"date": "2026-11-05", "instruction": "instructions/fallback.md",
               "headline": "A quiet night with one blocker.", "experiments": [],
               "claims_changed": [], "questions": [],
               "proposed_directions": [{"direction": "Free up disk space first.",
                                        "rationale": "Nothing else can run until then."}],
               "budget": {}, "open_blockers": []}
    (program / "sessions" / "2026-11-05.json").write_text(json.dumps(summary))
    code, out = check(program, capsys)
    assert code == 1 and "doesn't list open blockers B0001" in out


def test_next_id(program, capsys):
    run("next-id", "E", "--repo", program)
    assert capsys.readouterr().out.strip() == "E0001"
    append(program, "experiments.jsonl", registration("E0001"))
    run("next-id", "E", "--repo", program)
    assert capsys.readouterr().out.strip() == "E0002"
