"""The always-on service, end to end: a program with a remote, a stand-in
agent that writes the summary or review it is asked for, and (for job
events) the stand-in cluster from test_cluster."""

import json
import subprocess
import sys
from datetime import timedelta

import pytest

from test_cluster import slurm  # noqa: F401
from test_program import append, commit, git, isolated_git, line, program, run  # noqa: F401

from roborobo import config, service

STAND_IN = r'''
import json, os, re, subprocess, sys
prompt = sys.argv[-1]
lid = re.search(r"line (L\d{4})", prompt).group(1)
with open(os.environ["STAND_IN_PROMPTS"], "a") as f:
    f.write(prompt + "\n")
if "line-checker.md" in prompt:
    path = re.search(r"write your review to (reviews/\S+\.json)", prompt).group(1)
    doc = {"date": path[8:18], "line_id": lid, "reviewer_model": "stand-in",
           "summary": "Nothing to check.", "reviews": []}
else:
    path = re.search(r"write your summary to (sessions/\S+\.json)", prompt).group(1)
    followed = re.search(r'Set "instruction" in the summary to "([^"]+)"', prompt).group(1)
    doc = {"date": path[9:19], "line_id": lid, "instruction": followed,
           "headline": "A quiet run.", "experiments": [],
           "claims_changed": [], "questions": [], "budget": {}, "open_blockers": [],
           "proposed_directions": [{"direction": "Keep going.", "rationale": "No reason to stop."}]}
    if os.environ.get("STAND_IN_MODE") == "dirty":
        with open("PROGRAM.md", "a") as f:
            f.write("half-written\n")
with open(path, "w") as f:
    json.dump(doc, f)
subprocess.run(["git", "add", path], check=True)
subprocess.run(["git", "commit", "-q", "-m", f"{lid} run"], check=True)
'''


@pytest.fixture
def served(program, tmp_path, monkeypatch):
    """A program with L0001 and L0003 active and L0002 proposed, the
    stand-in agent, no gap between runs, and an approval."""
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN)
    monkeypatch.setenv("STAND_IN_PROMPTS", str(tmp_path / "prompts.txt"))
    with (program / "program.toml").open("a") as f:
        f.write(f'\n[runs]\nagent = ["{sys.executable}", "{script}"]\nworker_minutes = 1\n'
                '\n[service]\nmin_minutes_between_runs = 0\n')
    append(program, "lines.jsonl", line("L0001"))
    append(program, "lines.jsonl", line("L0002", by="agent", name="Spare heater",
                                        question="Does the spare drift?",
                                        reason="One is one sample.", status="proposed"))
    append(program, "lines.jsonl", line("L0003", name="Fan", question="Does the fan matter?"))
    commit(program, "lines")
    assert run("approve", "--repo", program) == 0
    git(program, "push", "-q", "origin", "main")
    return program


def conn(tmp_path):
    return service.connect(tmp_path / "state" / "service.db")


def look(program, db, after=timedelta()):
    return service.look(db, config.load(program), service.now_utc() + after)


def instruct(program, text, push=True):
    day = service.today(config.load(program), service.now_utc()).isoformat()
    (program / "instructions" / f"{day}.md").write_text(text)
    commit(program, "instruction")
    return day


def prompts(tmp_path):
    path = tmp_path / "prompts.txt"
    return path.read_text().splitlines() if path.exists() else []


def runs(db):
    return [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY id")]


def test_the_first_look_runs_nothing(served, tmp_path):
    instruct(served, "## L0001 Heater\n\n### Asked\n1. Is it the heater?\n")
    db = conn(tmp_path)
    look(served, db)
    assert runs(db) == [] and prompts(tmp_path) == []
    assert db.execute("SELECT status FROM events WHERE kind = 'instruction'").fetchone()[0] == "seen"


def test_a_new_instruction_runs_its_line_once_and_pushes(served, tmp_path):
    db = conn(tmp_path)
    look(served, db)
    day = instruct(served, "Keep it tidy.\n\n## L0001 Heater\n\n### Asked\n1. Is it the heater?\n")
    notes = look(served, db)
    (done,) = runs(db)
    assert done["stem"] == f"{day}-L0001" and json.loads(done["problems"]) == []
    assert (served / "sessions" / f"{day}-L0001.json").exists()
    assert (served / "reviews" / f"{day}-L0001.json").exists()
    worker, checker = prompts(tmp_path)
    assert f"write your summary to sessions/{day}-L0001.json" in worker
    assert f"`## L0001` section of instructions/{day}.md" in worker
    assert f"write your review to reviews/{day}-L0001.json" in checker
    assert any(n.startswith(f"ran {day}-L0001") for n in notes)
    assert git(served, "rev-parse", "HEAD") == git(served, "rev-parse", "origin/main")
    look(served, db)  # nothing new
    assert len(runs(db)) == 1


def test_a_changed_section_runs_again_with_a_numbered_summary(served, tmp_path):
    db = conn(tmp_path)
    look(served, db)
    day = instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n")
    look(served, db)
    instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n2. And the fan?\n")
    look(served, db)
    assert [r["stem"] for r in runs(db)] == [f"{day}-L0001", f"{day}-L0001-2"]
    assert (served / "sessions" / f"{day}-L0001-2.json").exists()


def test_runs_per_day_and_the_gap_between_them_are_limited(served, tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    noon = datetime.now(ZoneInfo("America/New_York")).replace(hour=12, minute=0, second=0,
                                                             microsecond=0)
    monkeypatch.setattr(service, "now_utc", lambda: noon)  # no midnight inside the test
    toml = served / "program.toml"
    toml.write_text(toml.read_text().replace("min_minutes_between_runs = 0",
                                             "min_minutes_between_runs = 60\n"
                                             "max_runs_per_line_per_day = 2"))
    commit(served, "limits")
    assert run("approve", "--repo", served) == 0
    db = conn(tmp_path)
    look(served, db)
    instruct(served, "## L0001 Heater\n\n1. One.\n")
    look(served, db)
    instruct(served, "## L0001 Heater\n\n1. Two.\n")
    notes = look(served, db, timedelta(minutes=30))
    assert len(runs(db)) == 1 and any("(minimum gap)" in n for n in notes)
    look(served, db, timedelta(minutes=61))
    assert len(runs(db)) == 2
    instruct(served, "## L0001 Heater\n\n1. Three.\n")
    notes = look(served, db, timedelta(minutes=200))
    assert len(runs(db)) == 2 and any("2 runs today (the limit)" in n for n in notes)


def test_what_the_scientist_pushes_is_merged_and_acted_on(served, tmp_path):
    db = conn(tmp_path)
    look(served, db)
    elsewhere = tmp_path / "laptop"
    origin = git(served, "remote", "get-url", "origin").strip()
    subprocess.run(["git", "clone", "-q", origin, str(elsewhere)], check=True)
    day = instruct(elsewhere, "## L0003 Fan\n\n1. Does the fan matter?\n")
    git(elsewhere, "push", "-q", "origin", "main")
    look(served, db)
    assert [r["stem"] for r in runs(db)] == [f"{day}-L0003"]
    git(elsewhere, "pull", "-q")
    assert (elsewhere / "sessions" / f"{day}-L0003.json").exists()  # pushed back


def test_challenges_start_a_run_and_proposed_lines_never_do(served, tmp_path):
    db = conn(tmp_path)
    look(served, db)
    review = {"date": "2026-11-17", "line_id": "L0003", "reviewer_model": "test",
              "summary": "One result, doubtful.",
              "reviews": [{"claim_id": "E0001", "plain_claim": "The fan matters.",
                           "verdict": "doubtful", "what_would_resolve": "A second fan.",
                           "attacks": [{"check": "other", "what_was_done": "Read the result file.",
                                        "finding": "Only one fan was tried."}]}]}
    (served / "reviews" / "2026-11-17-L0003.json").write_text(json.dumps(review))
    instruct(served, "## L0002 Spare heater\n\n1. Try the spare.\n")
    notes = look(served, db)
    assert [r["line"] for r in runs(db)] == ["L0003"]
    assert "open challenges in reviews/2026-11-17-L0003.json" in prompts(tmp_path)[0]
    assert "no new instruction for this line" in prompts(tmp_path)[0]
    assert '"none: started by the service for challenge"' in prompts(tmp_path)[0]
    assert any("L0002 isn't active" in n for n in notes)


def test_a_dirty_run_pauses_the_program_until_resumed(served, tmp_path, monkeypatch):
    db = conn(tmp_path)
    look(served, db)
    monkeypatch.setenv("STAND_IN_MODE", "dirty")
    instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n")
    notes = look(served, db)
    assert "paused after that run" in notes
    assert "paused" in look(served, db)[0]
    git(served, "checkout", "PROGRAM.md")
    assert run("serve", "--resume", "demo", "--state", tmp_path / "state") == 0
    assert not any(note.startswith("paused") for note in look(served, db))


def test_an_invalid_review_pauses_the_program(served, tmp_path):
    db = conn(tmp_path)
    look(served, db)
    (served / "reviews" / "2026-11-17-L0003.json").write_text('{"date": "2026-11-17"}')
    commit(served, "a bad review")
    notes = look(served, db)
    assert notes[0].startswith("paused: reviews don't validate")


def test_a_conflicting_push_pauses_the_program(served, tmp_path):
    db = conn(tmp_path)
    look(served, db)
    elsewhere = tmp_path / "laptop"
    origin = git(served, "remote", "get-url", "origin").strip()
    subprocess.run(["git", "clone", "-q", origin, str(elsewhere)], check=True)
    (elsewhere / "PROGRAM.md").write_text("one version\n")
    commit(elsewhere, "theirs")
    git(elsewhere, "push", "-q", "origin", "main")
    (served / "PROGRAM.md").write_text("another version\n")
    commit(served, "ours")
    notes = look(served, db)
    assert notes[0].startswith("paused: merging origin/main conflicts")
    assert service.uncommitted(config.load(served)) == []  # the merge was aborted


def test_an_ended_job_starts_its_lines_run(served, tmp_path, slurm):  # noqa: F811
    with (served / "program.toml").open("a") as f:
        f.write(f'\n[hosts.hatteras]\nkind = "slurm"\nssh = "ht1"\n'
                f'workdir = "{tmp_path / "cluster"}"\nslots = 4\n')
    append(served, "experiments.jsonl", {
        "record": "registration", "experiment_id": "E0001", "at": "2026-11-18T12:00:00+00:00",
        "by": "agent", "rq_id": "RQ1", "line_id": "L0001", "host": "hatteras",
        "hypothesis": "The heater causes the drift.", "method": "On hatteras, many seeds.",
        "judged_by": "The drift check on the heater runs.",
        "falsified_if": "No drift with the heater on.", "cost_estimate": {"cpu_hours": 1}})
    commit(served, "a host and an experiment")
    assert run("approve", "--repo", served) == 0
    db = conn(tmp_path)
    look(served, db)
    assert run("submit", "--repo", served, "--host", "hatteras", "--experiment", "E0001",
               "--", "true") == 0
    look(served, db)
    (done,) = runs(db)
    assert done["line"] == "L0001"
    assert "job hatteras-1001 (E0001) ended COMPLETED, artifact A0001" in prompts(tmp_path)[0]
    assert "Record ended jobs: hatteras-1001" in git(served, "log", "--format=%s")


def dashboard_page(served, tmp_path, db, notes=()):
    from roborobo import dashboard
    program = config.load(served)
    path = dashboard.write(tmp_path / "state", db, [program], {"demo": list(notes)}, {})
    return path.read_text()


def test_the_dashboard_shows_what_needs_you_and_each_line(served, tmp_path):
    from urllib.parse import parse_qs, urlparse
    import html
    import re
    db = conn(tmp_path)
    look(served, db)
    day = instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n")
    look(served, db)
    page = dashboard_page(served, tmp_path, db)
    assert "<b>L0002 proposed</b> by the agent: Spare heater. One is one sample." in page
    assert "<b>L0001</b><br>Heater" in page and "<b>L0003</b><br>Fan" in page
    assert "<b>L0002</b><br>" not in page  # only active lines get a row
    assert f"{day}-L0001" in page and "A quiet run." in page and "no claims" in page
    link = html.unescape(re.search(r"href='(claude://code/new\?[^']+)'", page).group(1))
    query = parse_qs(urlparse(link).query)
    assert query["folder"] == [str(served)]
    assert "Run `git pull` first" in query["q"][0]
    assert f"sessions/{day}-L0001.json" in query["q"][0]
    assert (tmp_path / "state" / "dashboard" / "demo" / "map.html").exists()


def test_the_dashboard_shows_a_pause_with_how_to_resume(served, tmp_path, monkeypatch):
    db = conn(tmp_path)
    look(served, db)
    monkeypatch.setenv("STAND_IN_MODE", "dirty")
    instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n")
    look(served, db)
    page = dashboard_page(served, tmp_path, db, ["job problem: E0001 job 7 ended FAILED"])
    assert "<b>Paused</b>" in page and "program serve --resume demo" in page
    assert "job problem: E0001 job 7 ended FAILED" in page


def test_the_dashboard_is_served(tmp_path):
    from urllib.request import urlopen
    from roborobo import dashboard
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "index.html").write_text("<h1>hello</h1>")
    server = dashboard.serve_http(tmp_path, "127.0.0.1", 0)
    try:
        port = server.server_address[1]
        response = urlopen(f"http://127.0.0.1:{port}/")
        assert response.read() == b"<h1>hello</h1>"
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        server.shutdown()


def test_later_runs_sort_after_earlier_ones():
    from roborobo.dashboard import run_order
    names = ["sessions/2026-11-23-L0013-2.json", "sessions/2026-11-23-L0013.json",
             "sessions/2026-11-22-L0013-3.json"]
    assert sorted(names, key=run_order) == [
        "sessions/2026-11-22-L0013-3.json", "sessions/2026-11-23-L0013.json",
        "sessions/2026-11-23-L0013-2.json"]


def test_questions_answered_by_a_later_instruction_are_not_shown(served, tmp_path):
    from roborobo import dashboard
    asked = {"date": "2026-11-17", "line_id": "L0001", "instruction": "none",
             "headline": "A run with a question.", "experiments": [], "claims_changed": [],
             "questions": [{"question": "Should the heater go higher?",
                            "why_it_matters": "It decides the next run.",
                            "default_if_unanswered": "Keep it where it is."}],
             "budget": {}, "open_blockers": [],
             "proposed_directions": [{"direction": "Keep going.", "rationale": "No reason to stop."}]}
    (served / "sessions" / "2026-11-17-L0001.json").write_text(json.dumps(asked))
    commit(served, "a run that asks")
    db = conn(tmp_path)
    assert "Should the heater go higher?" in dashboard_page(served, tmp_path, db)
    import time
    time.sleep(1.1)  # commit times are in whole seconds
    instruct(served, "## L0001 Heater\n\n1. Yes, higher.\n")
    assert "Should the heater go higher?" not in dashboard_page(served, tmp_path, db)
