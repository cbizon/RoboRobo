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
    line_run = (program / "scheduled" / "line.md").read_text()
    assert "sessions/YYYY-MM-DD-L0013.json" in line_run and "{{" not in line_run
    plist = (program / "scheduled" / "run-night.plist").read_text()
    assert "<integer>23</integer>" in plist and "program run-night --repo" in plist
    assert 'export PATH="$HOME/.local/bin:$PATH"' in plist
    assert not (program / "scheduled" / "session.md").exists()
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
        f.write("  cpu_hours:\n    - {max: 999999, per: week, where: [local]}\n")
    import roborobo.approvals as ap
    from roborobo import config
    prog = config.load(program)
    (program / "approvals.json").write_text(json.dumps({"files": ap.manifest(prog)}))
    commit(program, "forged")
    git(program, "tag", "approved/20990101T000000Z")
    code, out = check(program, capsys)
    assert code == 1 and "budgets.yaml changed since approval" in out


def budget_problems(program, text, hosts=""):
    from roborobo import config
    from roborobo.records import budgets, load_ledger
    (program / "budgets.yaml").write_text(text)
    if hosts:
        with (program / "program.toml").open("a") as f:
            f.write(hosts)
    prog = config.load(program)
    return budgets(prog, load_ledger(prog)[0])


HATTERAS = '\n[hosts.hatteras]\nkind = "slurm"\nssh = "h"\nworkdir = "/w"\nslots = 1\n'


def test_budgets_template_is_valid(program):
    assert budget_problems(program, (program / "budgets.yaml").read_text()) == ([], [])


def test_budgets_with_hosts(program):
    problems, report = budget_problems(program, (
        "budgets:\n"
        "  cpu_hours:\n"
        "    - {max: 4000, per: week, where: [local, hatteras]}\n"
        "    - {max: 100, per: run, where: [local, hatteras]}\n"
        "  cores:\n"
        "    - {max: 10, per: instant, where: [local]}\n"
        "  disk_free_gb:\n"
        "    - {min: 40, per: instant, where: [local]}\n"), HATTERAS)
    assert problems == []
    assert "cpu_hours: 0 of 4000 per week (0%)" in report
    assert ("disk_free_gb: at least 40 per instant on local (watched by the agent, "
            "not summed from the ledger)") in report


@pytest.mark.parametrize("text, message", [
    ("budgets:\n  cpu_hours: {limit: 1, period: week}\n", "old {limit, period} form"),
    ("budgets:\n  cpu_hours:\n    - {max: 1, per: week, where: [ashe]}\n",
     "where names ashe, which is neither local nor a host"),
    ("budgets:\n  cpu_hours:\n    - {max: 1, per: week, where: [local]}\n",
     "must cover every place (hatteras, local)"),
    ("budgets:\n  disk_free_gb:\n    - {min: 40, per: week, where: [local, hatteras]}\n",
     "budgets.yaml: budgets/disk_free_gb/0"),
    ("budgets:\n  cores:\n    - {max: 1, min: 0, per: instant, where: [local]}\n",
     "budgets.yaml: budgets/cores/0"),
    ("budgets:\n  cores:\n    - {max: 1, per: instant}\n", "'where' is a required property"),
    ("budgets:\n  cores:\n    - {max: 1, per: instant, where: [local], note: x}\n",
     "Additional properties are not allowed"),
    ("budgets: [\n", "not valid YAML"),
])
def test_budget_rules_are_checked(program, text, message):
    problems, _ = budget_problems(program, text, HATTERAS)
    assert any(message in p for p in problems), problems


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
    (program / "budgets.yaml").write_text(
        "budgets:\n  cpu_hours:\n    - {max: 1, per: program, where: [local]}\n")
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


def test_summary_answers_validate(program, capsys):
    run("approve", "--repo", program)
    summary = {"date": "2026-11-05", "instruction": "instructions/2026-11-05.md",
               "answers": [{"asked": "Does turning the heater off stop the drift?",
                            "status": "answered", "answer": "Yes, at all three settings."}],
               "headline": "The heater causes the drift.", "experiments": [],
               "claims_changed": [], "questions": [],
               "proposed_directions": [{"direction": "Repeat with the spare heater.",
                                        "rationale": "One heater is one sample."}],
               "budget": {}, "open_blockers": []}
    path = program / "sessions" / "2026-11-05.json"
    path.write_text(json.dumps(summary))
    assert check(program, capsys)[0] == 0
    summary["answers"][0]["status"] = "reframed"
    path.write_text(json.dumps(summary))
    code, out = check(program, capsys)
    assert code == 1 and "answers/0/status" in out


def line(lid, record="open", **extra):
    base = {"record": record, "line_id": lid, "at": "2026-11-05T23:00:00+00:00", "by": "human"}
    if record == "open":
        base |= {"name": "Heater", "question": "Does the heater cause the drift?"}
    return base | extra


def test_experiments_belong_to_a_line_once_lines_exist(program, capsys):
    run("approve", "--repo", program)
    append(program, "experiments.jsonl", registration("E0001", at="2026-11-04T23:10:00+00:00"))
    append(program, "lines.jsonl", line("L0001"))
    append(program, "experiments.jsonl", registration("E0002", line_id="L0001", title="Heater off"))
    assert check(program, capsys)[0] == 0
    append(program, "experiments.jsonl", registration("E0003"))
    append(program, "experiments.jsonl", registration("E0004", line_id="L0009"))
    append(program, "lines.jsonl", line("L0002", "note", night="2026-11-05",
                                        text="Nothing was opened first."))
    code, out = check(program, capsys)
    assert code == 1
    assert "E0003 has no line_id" in out
    assert "E0004 names L0009, which isn't a line" in out
    assert "L0002 has a note before it is opened" in out


def test_sync_creates_new_ledger_files(program, capsys):
    run("approve", "--repo", program)
    (program / "ledger" / "lines.jsonl").unlink()
    code, out = check(program, capsys)
    assert code == 1 and "ledger/lines.jsonl is missing" in out
    assert run("sync", "--repo", program) == 0
    assert check(program, capsys)[0] == 0


def test_map_is_built_from_the_records(program, tmp_path, capsys):
    from roborobo import config, mapview
    append(program, "experiments.jsonl", registration("E0001", at="2026-11-04T23:10:00+00:00"))
    append(program, "lines.jsonl", line("L0001", experiments=[
        {"experiment_id": "E0001", "title": "Heater on, as a baseline"}]))
    append(program, "lines.jsonl", line("L0002", by="agent", name="Spare heater", parent="L0001",
                                        question="Does the spare heater drift too?",
                                        reason="One heater is one sample."))
    append(program, "experiments.jsonl", registration("E0002", line_id="L0002", title="Spare heater on"))
    append(program, "lines.jsonl", line("L0001", "note", by="agent", night="2026-11-05",
                                        text="The drift stops with the heater off."))
    append(program, "lines.jsonl", line("L0001", "update", by="agent", feeds=["L0002"],
                                        state="The heater causes the drift at all three settings.",
                                        artifacts=[{"kind": "evidence", "path": "E0001-results.json"}]))
    append(program, "lines.jsonl", line("L0002", "note", night="2026-11-06", waiting=True,
                                        text="Waiting for the spare to arrive."))
    append(program, "terms.jsonl", {"term_id": "T0001", "at": "2026-11-05T23:00:00+00:00",
                                    "by": "agent", "term": "Drift",
                                    "definition": "A slow change in the reading.",
                                    "aliases": ["drifts"]})
    summary = {"date": "2026-11-05", "instruction": "instructions/fallback.md",
               "answers": [{"asked": "Does turning the heater off stop the drift?",
                            "status": "answered", "answer": "Yes, at all three settings."}],
               "headline": "The heater causes the drift.",
               "experiments": [{"experiment_id": "E0001", "question": "Does the heater matter?",
                                "what_was_done": "Ran with it on and off.",
                                "result": "The drift stopped.", "takeaway": "It is the heater."}],
               "claims_changed": [], "questions": [],
               "proposed_directions": [{"direction": "Repeat with the spare heater.",
                                        "rationale": "One heater is one sample."}],
               "budget": {}, "open_blockers": []}
    (program / "sessions" / "2026-11-05.json").write_text(json.dumps(summary))
    review = {"date": "2026-11-05", "reviewer_model": "test", "summary": "One result, checked.",
              "reviews": [{"claim_id": "E0001", "plain_claim": "The heater causes the drift.",
                           "verdict": "doubtful", "what_would_resolve": "A second heater.",
                           "attacks": [{"check": "other", "what_was_done": "Read the result file.",
                                        "finding": "Only one heater was tried."}]}],
              "answers": [{"asked": "Does turning the heater off stop the drift?",
                           "verdict": "answered", "finding": "The runs match the answer."}],
              "run_conditions": {"verdict": "strained", "assessment": "The disk was nearly full."}}
    (program / "reviews" / "2026-11-05.json").write_text(json.dumps(review))

    model = mapview.build(config.load(program))
    assert [n["d"] for n in model["nights"]] == ["2026-11-05", "2026-11-06"]
    assert model["nights"][0]["run"] == "warn" and model["nights"][0]["fallback"]
    heater, spare = model["lines"]
    assert (heater["name"], heater["by"], heater["depth"], heater["feeds"]) == ("Heater", "Pat", 0, ["L0002"])
    assert heater["nights"]["2026-11-05"] == {
        "k": "doubtful", "e": ["E0001"],
        "notes": [{"t": "The drift stops with the heater off.", "by": "the agent"}]}
    assert heater["arts"] == [["evidence", "E0001-results.json"]]
    assert (spare["parent"], spare["depth"], spare["by"]) == ("L0001", 1, "the agent")
    assert spare["nights"]["2026-11-06"]["k"] == "waiting" and spare["other"] == ["E0002"]
    assert model["exps"]["E0001"] == {"title": "Heater on, as a baseline", "status": None,
                                      "chk": "doubtful"}
    assert model["asks"]["items"][0]["verdict"] == "answered"
    assert model["terms"][0]["aliases"] == ["drifts"] and model["unassigned"] == []

    out = tmp_path / "page" / "map.html"
    assert run("map", "--repo", program, "--out", out) == 0
    page = out.read_text()
    assert "<title>Demo program map</title>" in page and "Spare heater" in page
    assert "/*DATA*/" not in page


def test_next_id(program, capsys):
    run("next-id", "E", "--repo", program)
    assert capsys.readouterr().out.strip() == "E0001"
    append(program, "experiments.jsonl", registration("E0001"))
    run("next-id", "E", "--repo", program)
    assert capsys.readouterr().out.strip() == "E0002"


# The night runner, with a stand-in for the agent command: a script that
# writes the summary or review it was asked for and commits it, the way a
# real run ends.

STAND_IN = r'''
import json, os, re, subprocess, sys
prompt = sys.argv[-1]
lid = re.search(r"line (L\d{4})", prompt).group(1)
night = re.search(r"night of (\d{4}-\d{2}-\d{2})", prompt).group(1)
with open(os.environ["STAND_IN_PROMPTS"], "a") as f:
    f.write(prompt + "\n")
if "line-checker.md" in prompt:
    path = f"reviews/{night}-{lid}.json"
    doc = {"date": night, "line_id": lid, "reviewer_model": "stand-in",
           "summary": "Nothing to check.", "reviews": []}
else:
    path = f"sessions/{night}-{lid}.json"
    doc = {"date": night, "line_id": lid, "instruction": f"instructions/{night}.md",
           "headline": "A quiet run.", "experiments": [], "claims_changed": [],
           "questions": [], "budget": {}, "open_blockers": [],
           "proposed_directions": [{"direction": "Keep going.", "rationale": "No reason to stop."}]}
    if os.environ.get("STAND_IN_MODE") == "dirty":
        with open("PROGRAM.md", "a") as f:
            f.write("half-written\n")
    if os.environ.get("STAND_IN_MODE") == "fail":
        sys.exit(3)
with open(path, "w") as f:
    json.dump(doc, f)
subprocess.run(["git", "add", path], check=True)
subprocess.run(["git", "commit", "-q", "-m", f"{lid} run"], check=True)
'''

CHALLENGED_REVIEW = {
    "date": "2026-11-17", "line_id": "L0003", "reviewer_model": "test",
    "summary": "One result, doubtful.",
    "reviews": [{"claim_id": "E0001", "plain_claim": "The heater causes the drift.",
                 "verdict": "doubtful", "what_would_resolve": "A second heater.",
                 "attacks": [{"check": "other", "what_was_done": "Read the result file.",
                              "finding": "Only one heater was tried."}]}]}


@pytest.fixture
def night_program(program, tmp_path, monkeypatch):
    """A program with three lines (L0001 active, L0002 proposed, L0003 active
    with an open challenge), the stand-in agent, and an approval."""
    import sys
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN)
    monkeypatch.setenv("STAND_IN_PROMPTS", str(tmp_path / "prompts.txt"))
    with (program / "program.toml").open("a") as f:
        f.write(f'\n[runs]\nagent = ["{sys.executable}", "{script}"]\nworker_minutes = 1\n')
    append(program, "lines.jsonl", line("L0001"))
    append(program, "lines.jsonl", line("L0002", by="agent", name="Spare heater",
                                        question="Does the spare drift?", reason="One is one sample.",
                                        status="proposed"))
    append(program, "lines.jsonl", line("L0003", name="Fan", question="Does the fan matter?"))
    (program / "reviews" / "2026-11-17-L0003.json").write_text(json.dumps(CHALLENGED_REVIEW))
    (program / "instructions" / "2026-11-18.md").write_text(
        "Keep the disk clean.\n\n## L0001 Heater\n\n### Asked\n1. Is it the heater?\n\n"
        "## L0002 Spare heater\n\n### Asked\n1. Try the spare.\n")
    commit(program, "lines and tonight's instruction")
    assert run("approve", "--repo", program) == 0
    return program


def tonight(hour=23, minute=30):
    from datetime import datetime
    return lambda: datetime(2026, 11, 18, hour, minute)


def test_night_plan_runs_instructed_and_challenged_lines(night_program, capsys):
    # A dry run fails when an instructed line won't run, so the debrief sees it.
    assert run("run-night", "--repo", night_program, "--night", "2026-11-18", "--dry-run") == 1
    out = capsys.readouterr().out
    assert "L0001: instructed" in out
    assert "L0003: open challenges in reviews/2026-11-17-L0003.json" in out
    assert "skipped: instructions/2026-11-18.md has a section for L0002, which is proposed" in out


def test_night_runs_each_line_then_its_checker(night_program, tmp_path):
    from datetime import date
    from roborobo import config, night
    program = config.load(night_program)
    code, text = night.run_night(program, date(2026, 11, 18), only=["L0001", "L0003"],
                                 now=tonight())
    assert code == 0, text
    for lid in ("L0001", "L0003"):
        assert (night_program / "sessions" / f"2026-11-18-{lid}.json").exists()
        assert (night_program / "reviews" / f"2026-11-18-{lid}.json").exists()
    prompts = (tmp_path / "prompts.txt").read_text().splitlines()
    assert [p.split(" of the ")[0] for p in prompts] == [
        "Run line L0001", "Check line L0001's run", "Run line L0003", "Check line L0003's run"]
    assert "`## L0001` section of instructions/2026-11-18.md" in prompts[0]
    assert "no section for this line" in prompts[2]
    assert "## L0003 (open challenges" in text
    logs = tmp_path / "demo-data" / "logs" / "2026-11-18"
    assert (logs / "night.md").read_text() == text
    assert (logs / "L0001-worker.log").exists()


def test_proposed_line_makes_the_night_report_a_problem(night_program):
    from datetime import date
    from roborobo import config, night
    code, text = night.run_night(config.load(night_program), date(2026, 11, 18), now=tonight())
    assert code == 1 and "which is proposed" in text
    assert (night_program / "sessions" / "2026-11-18-L0003.json").exists()


def test_dirty_run_stops_the_night(night_program, monkeypatch):
    from datetime import date
    from roborobo import config, night
    monkeypatch.setenv("STAND_IN_MODE", "dirty")
    code, text = night.run_night(config.load(night_program), date(2026, 11, 18),
                                 only=["L0001", "L0003"], now=tonight())
    assert code == 1
    assert "the worker left uncommitted changes" in text
    assert "The night stopped early" in text and "Not started: L0003" in text
    assert not (night_program / "reviews" / "2026-11-18-L0001.json").exists()


def test_failed_run_is_reported_and_the_night_goes_on(night_program, monkeypatch):
    from datetime import date
    from roborobo import config, night
    monkeypatch.setenv("STAND_IN_MODE", "fail")
    code, text = night.run_night(config.load(night_program), date(2026, 11, 18),
                                 only=["L0001", "L0003"], now=tonight())
    assert code == 1
    assert "worker exited with code 3" in text and "the worker wrote no sessions/" in text
    assert "## L0003" in text and "stopped early" not in text


def test_no_line_starts_after_the_session_ends(night_program):
    from datetime import date, datetime
    from roborobo import config, night
    morning = lambda: datetime(2026, 11, 19, 5, 30)
    (night_program / "instructions" / "2026-11-18.md").write_text("## L0001 Heater\n")
    commit(night_program, "only L0001 tonight")
    code, text = night.run_night(config.load(night_program), date(2026, 11, 18), now=morning)
    assert code == 1 and "Not started: L0001, L0003" in text
    # A line asked for by name runs whenever it's asked for.
    code, text = night.run_night(config.load(night_program), date(2026, 11, 18),
                                 only=["L0001"], now=morning)
    assert code == 0, text


def test_night_of_and_instruction_sections(program):
    from datetime import date, datetime
    from roborobo import config, night
    p = config.load(program)
    assert night.night_of(p, datetime(2026, 11, 19, 1, 0)) == date(2026, 11, 18)
    assert night.night_of(p, datetime(2026, 11, 18, 23, 0)) == date(2026, 11, 18)
    assert night.instructed_lines("## L0002 A\n## L0001 B\n", "x") == ["L0002", "L0001"]
    (program / "instructions" / "2026-11-18.md").write_text("## Asked\n1. Anything.\n")
    assert "has no line sections" in night.plan(p, date(2026, 11, 18)).skipped[0]
    with pytest.raises(ValueError, match="more than one section for L0001"):
        night.instructed_lines("## L0001 A\n## L0001 again\n", "x")


def test_sync_removes_retired_files(program, capsys):
    run("approve", "--repo", program)
    (program / "scheduled" / "session.md").write_text("Run tonight's session.\n")
    code, out = check(program, capsys)
    assert code == 1 and "scheduled/session.md differs from RoboRobo's template" in out
    assert run("sync", "--repo", program) == 0
    assert not (program / "scheduled" / "session.md").exists()
    assert check(program, capsys)[0] == 0


def test_missing_agent_command_stops_the_night_before_any_line(night_program, monkeypatch):
    from datetime import date
    from roborobo import config, night
    program = config.load(night_program)
    program.runs["agent"] = ["no-such-agent-command", "-p"]
    code, text = night.run_night(program, date(2026, 11, 18), only=["L0001"], now=tonight())
    assert code == 1
    assert "the agent command 'no-such-agent-command' isn't on this PATH" in text
    assert "Not started: L0001" in text and "## L0001" not in text
