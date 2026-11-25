"""The always-on service, end to end: a program with a remote, a stand-in
agent that writes the summary or review it is asked for, and (for job
events) the stand-in cluster from test_cluster."""

import json
import re
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
    mode = os.environ.get("STAND_IN_MODE")
    if mode == "limit":
        print("You've hit your limit · resets 4:10pm (UTC)")
        sys.exit(1)
    if mode == "fail":
        sys.exit(3)
    if mode == "dirty":
        with open("PROGRAM.md", "a") as f:
            f.write("half-written\n")
    if mode == "note":  # append a ledger record, as every real run does
        with open("ledger/lines.jsonl", "a") as f:
            f.write(json.dumps({"record": "note", "line_id": lid, "at": "2026-11-23T12:00:00Z",
                                "by": "agent", "night": path[9:19],
                                "text": f"{lid} ran in its own worktree."}) + "\n")
        subprocess.run(["git", "add", "ledger/lines.jsonl"], check=True)
    if mode == "register":  # an agent that registers E0001 without asking for an id
        with open("ledger/experiments.jsonl", "a") as f:
            f.write(json.dumps({"record": "registration", "experiment_id": "E0001",
                                "at": "2026-11-23T12:00:00Z", "by": "agent", "rq_id": "RQ1",
                                "line_id": lid, "hypothesis": "The heater causes the drift.",
                                "method": "Turn the heater off and on.",
                                "judged_by": "The drift check on both runs.",
                                "falsified_if": "The drift stays with the heater off.",
                                "cost_estimate": {"cpu_hours": 1}}) + "\n")
        subprocess.run(["git", "add", "ledger/experiments.jsonl"], check=True)
    if mode == "same-file":  # two lines writing the same new file differently
        with open("method.md", "w") as f:
            f.write(f"# Method\n\nHow it is done, as {lid} sees it.\n")
        subprocess.run(["git", "add", "method.md"], check=True)
    if mode == "section":  # each line adds its own section to PROGRAM.md
        with open("PROGRAM.md", "a") as f:
            f.write(f"\n### {lid}\n\nWhere {lid} stands after this run.\n")
        subprocess.run(["git", "add", "PROGRAM.md"], check=True)
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


def ran_on(program, lid, day):
    """A summary for the line, committed a second later: the night runner ran it."""
    import time
    time.sleep(1.1)  # commit times are in whole seconds
    doc = {"date": day, "line_id": lid, "instruction": f"instructions/{day}.md",
           "headline": "The night runner's run.", "experiments": [], "claims_changed": [],
           "questions": [], "budget": {}, "open_blockers": [],
           "proposed_directions": [{"direction": "Keep going.", "rationale": "No reason to stop."}]}
    (program / "sessions" / f"{day}-{lid}.json").write_text(json.dumps(doc))
    commit(program, f"{lid} ran")


def test_the_first_look_skips_what_already_ran(served, tmp_path):
    day = instruct(served, "## L0001 Heater\n\n### Asked\n1. Is it the heater?\n")
    ran_on(served, "L0001", day)
    db = conn(tmp_path)
    look(served, db)
    assert runs(db) == [] and prompts(tmp_path) == []
    assert db.execute("SELECT status FROM events WHERE kind = 'instruction'").fetchone()[0] == "seen"


def test_the_first_look_runs_what_never_ran(served, tmp_path):
    """The night runner was turned off before it reached tonight's instruction."""
    day = instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n\n"
                           "## L0003 Fan\n\n1. Does the fan matter?\n")
    ran_on(served, "L0001", day)
    db = conn(tmp_path)
    look(served, db)
    assert [r["line"] for r in runs(db)] == ["L0003"]


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


def test_a_dirty_run_pauses_its_line_until_resumed(served, tmp_path, monkeypatch):
    """Lines run in worktrees of their own, so a broken run holds up only its
    line, not the program."""
    db = conn(tmp_path)
    look(served, db)
    monkeypatch.setenv("STAND_IN_MODE", "dirty")
    instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n")
    notes = look(served, db)
    assert "paused L0001" in notes
    instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n2. Still?\n")
    assert any("L0001 is paused" in n for n in look(served, db))
    git(served.parent / "demo.L0001", "checkout", "PROGRAM.md")
    assert run("serve", "--resume", "demo:L0001", "--state", tmp_path / "state") == 0
    monkeypatch.delenv("STAND_IN_MODE")
    assert not any("paused" in note for note in look(served, db))


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
    (elsewhere / "method.md").write_text("one version\n")
    commit(elsewhere, "theirs")
    git(elsewhere, "push", "-q", "origin", "main")
    (served / "method.md").write_text("another version\n")
    commit(served, "ours")
    notes = look(served, db)
    assert notes[0].startswith("paused: merging origin/main conflicts")
    assert service.uncommitted(config.load(served)) == []  # the merge was aborted


def test_an_ended_job_gets_its_id_from_the_services_database(served, tmp_path, slurm,  # noqa: F811
                                                            monkeypatch):
    """Nov 24: the service crashed with "database is locked" when a job ended in
    the same look as a new instruction: the id came from a second connection
    while the first had the new event uncommitted."""
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
    monkeypatch.setenv("ROBOROBO_IDS", str(tmp_path / "state" / "service.db"))  # as serve sets it
    monkeypatch.setattr("roborobo.records.RESERVE_TIMEOUT", 3)
    assert run("submit", "--repo", served, "--host", "hatteras", "--experiment", "E0001",
               "--", "true") == 0
    instruct(served, "## L0003 Fan\n\n1. Does the fan matter?\n")  # a new event, same look
    look(served, db)  # no "database is locked"
    look(served, db)  # one line per look by default
    assert sorted(r["line"] for r in runs(db)) == ["L0001", "L0003"]


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
    assert "<form" not in page and "<textarea" not in page  # a list: nothing is answered here
    waiting = page.split("<h3>Waiting on you (1)</h3>")[1].split("<h3>")[0]
    assert "<b>L0002</b> · Spare heater</a>" in waiting and "Proposed by the agent" in waiting
    assert "<a href='demo/lines/L0001.html'><b>L0001</b> · Heater</a><span class=new hidden>new</span>" in page
    assert "<a href='demo/lines/L0003.html'><b>L0003</b> · Fan</a><span class=new hidden>new</span>" in page
    assert f"Last run {day}: checker" in row(page, "L0001") and "no claims" in row(page, "L0001")
    heater = line_page_of(tmp_path, "L0001")
    assert f"{day}-L0001" in heater and "A quiet run." in heater
    assert "Nothing to check." in heater  # the checker's view of that run, on the line's page
    link = html.unescape(re.search(r"href='(claude://code/new\?[^']+)'", heater).group(1))
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
    assert "Stopped: needs fixing" in row(page, "L0001")
    assert "<h3>Stopped: needs fixing (1)</h3>" in page
    assert "program serve --resume demo:L0001" in line_page_of(tmp_path, "L0001")
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
    index = dashboard_page(served, tmp_path, db)
    assert "Should the heater go higher?" not in index  # questions are on the line's page only
    assert "0 of 1 question answered" in row(index, "L0001")
    asks = line_page_of(tmp_path, "L0001")
    assert "<b>Question 1 of 1.</b> Should the heater go higher?" in asks
    assert "Why it matters: It decides the next run." in asks
    assert "If you don&#x27;t answer: Keep it where it is." in asks or \
        "If you don't answer: Keep it where it is." in asks
    import time
    time.sleep(1.1)  # commit times are in whole seconds
    instruct(served, "## L0001 Heater\n\n1. Yes, higher.\n")
    dashboard_page(served, tmp_path, db)
    assert "<b>Question 1 of 1.</b>" not in line_page_of(tmp_path, "L0001")


def test_each_line_has_a_page_with_its_documents(served, tmp_path):
    from roborobo import dashboard
    line_dir = served / "lines" / "L0001"
    line_dir.mkdir(parents=True)
    (line_dir / "progress.md").write_text("# Progress\n\n- **Done:** the heater test.\n")
    (line_dir / "design.md").write_text("# Design\n\n<script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    long_state = "The heater drives the drift. " * 12
    append(served, "lines.jsonl", line("L0001", "update", state=long_state,
                                       artifacts=[{"kind": "evidence", "path": "evidence/E0001.json"},
                                                  {"kind": "code branch", "path": "method/heater"}]))
    now = service.now_utc().isoformat()
    append(served, "lines.jsonl", line("L0001", "note", at=now, night=now[:10],
                                       text="Heater results are in."))
    (served / "evidence").mkdir(exist_ok=True)
    (served / "evidence" / "E0001.json").write_text('{"drift": 0.4}')
    commit(served, "a line with documents")
    db = conn(tmp_path)
    index = dashboard_page(served, tmp_path, db)
    assert "(today)" in row(index, "L0001")  # last heard

    folder = tmp_path / "state" / "dashboard" / "demo"
    line_page = (folder / "lines" / "L0001.html").read_text()
    assert "<h1>L0001 · Heater</h1>" in line_page
    assert long_state.strip() in line_page  # in full, not cut
    assert "<b>Next:</b> <span class=none>not recorded</span>" in line_page
    assert "<strong>Done:</strong> the heater test." in line_page  # progress, rendered
    assert "href='../docs/lines/L0001/design.md.html'" in line_page
    assert "href='../docs/evidence/E0001.json.html'" in line_page
    assert "<code>method/heater</code>" in line_page  # a branch: named, no page
    design = (folder / "docs" / "lines" / "L0001" / "design.md.html").read_text()
    assert "<h1>Design</h1>" in design and "<td>1</td>" in design
    assert "&lt;script&gt;" in design and "<script>alert" not in design
    evidence = (folder / "docs" / "evidence" / "E0001.json.html").read_text()
    assert '&quot;drift&quot;: 0.4' in evidence
    assert "href='../../lines/L0001.html'" in evidence  # back to the line


def test_a_quiet_line_is_marked(served, tmp_path):
    old = "2026-01-02T03:04:05+00:00"
    append(served, "lines.jsonl", line("L0003", "note", at=old, night="2026-01-02",
                                       text="A note from long ago."))
    commit(served, "an old note")
    page = dashboard_page(served, tmp_path, conn(tmp_path))
    assert "class=warn" in row(page, "L0003") and "days ago" in row(page, "L0003")


def test_lists_right_after_a_paragraph_render_as_lists():
    from roborobo.dashboard import render_markdown
    out = render_markdown("The hold step:\n1. Walk the graph.\n2. Advance members.\n\n"
                          "```\nnot a list:\n1. stays code\n```\n")
    assert "<ol>" in out and "<li>Walk the graph.</li>" in out
    assert "<code>not a list:\n1. stays code\n</code>" in out


def test_the_usage_limit_holds_the_program_and_keeps_the_event(served, tmp_path, monkeypatch):
    db = conn(tmp_path)
    look(served, db)
    monkeypatch.setenv("STAND_IN_MODE", "limit")
    instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n")
    look(served, db)
    (attempt,) = runs(db)
    assert attempt["counted"] == 0 and attempt["summary"] is None
    assert db.execute("SELECT status FROM events WHERE kind = 'instruction'").fetchone()[0] == "pending"
    notes = look(served, db)
    assert any("waiting for the Claude usage limit; runs start again at" in n for n in notes)
    assert len(runs(db)) == 1  # nothing starts while the limit holds
    monkeypatch.delenv("STAND_IN_MODE")
    look(served, db, timedelta(hours=25))
    assert [r["counted"] for r in runs(db)] == [0, 1]


def test_the_limit_reset_time_is_read_from_the_message(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from roborobo.service import LIMIT
    found = LIMIT.search("You've hit your limit · resets 4:10pm (UTC)")
    assert (found[1], found[2], found[3], found[4]) == ("4", "10", "pm", "UTC")
    assert LIMIT.search("hit your limit, resets 9am (America/New_York)")[4] == "America/New_York"


def test_failed_runs_have_their_own_cap_and_dont_use_the_days_runs(served, tmp_path, monkeypatch):
    db = conn(tmp_path)
    look(served, db)
    monkeypatch.setenv("STAND_IN_MODE", "fail")
    for n in (1, 2, 3):
        instruct(served, f"## L0001 Heater\n\n1. Attempt {n}.\n")
        notes = look(served, db)
    assert [r["counted"] for r in runs(db)] == [0, 0]  # the third didn't start
    assert any("2 runs today that ended without a summary (the limit)" in n for n in notes)


def test_a_line_can_set_a_timer_to_be_started_again(served, tmp_path, monkeypatch):
    db = conn(tmp_path)
    look(served, db)
    prog = config.load(served)
    database = tmp_path / "state" / "service.db"
    soon = service.now_utc() + timedelta(minutes=30)
    service.set_timer(database, prog, "L0001", soon, "collect job 1234")
    look(served, db)
    assert runs(db) == []  # not due yet
    real = service.now_utc
    monkeypatch.setattr(service, "now_utc", lambda: real() + timedelta(minutes=31))
    look(served, db)
    assert [r["line"] for r in runs(db)] == ["L0001"]
    assert "the timer this line set: collect job 1234" in prompts(tmp_path)[0]
    monkeypatch.setattr(service, "now_utc", real)
    with pytest.raises(SystemExit, match="between 10 minutes and 7 days"):
        service.set_timer(database, prog, "L0001", real() + timedelta(minutes=2), "too soon")
    for n in range(3):
        service.set_timer(database, prog, "L0003", real() + timedelta(hours=1 + n), f"t{n}")
    with pytest.raises(SystemExit, match="already has 3 timers waiting"):
        service.set_timer(database, prog, "L0003", real() + timedelta(hours=5), "one too many")


def test_wake_needs_the_service(served, monkeypatch):
    monkeypatch.delenv("ROBOROBO_IDS", raising=False)
    assert "timers belong to the always-on service" in run(
        "wake", "--repo", served, "--line", "L0001", "--in-minutes", "30", "--reason", "x")


def test_wake_under_the_service_sets_a_timer(served, tmp_path, monkeypatch, capsys):
    db = conn(tmp_path)
    monkeypatch.setenv("ROBOROBO_IDS", str(tmp_path / "state" / "service.db"))
    assert run("wake", "--repo", served, "--line", "L0001", "--in-minutes", "45",
               "--reason", "collect the pilot") == 0
    assert "L0001 will be started at" in capsys.readouterr().out
    assert db.execute("SELECT line, reason, fired FROM timers").fetchone()[:] == (
        "L0001", "collect the pilot", 0)



def test_the_service_keeps_rendered_files_in_step_with_roborobo(served, tmp_path):
    db = conn(tmp_path)
    look(served, db)
    with (served / "CLAUDE.md").open("a") as f:
        f.write("\nAn older RoboRobo rendered this differently.\n")
    (served / "scheduled" / "session.md").write_text("retired\n")
    commit(served, "as an older RoboRobo left it")
    notes = look(served, db)
    assert notes[0].startswith("synced from RoboRobo: CLAUDE.md")
    assert "An older RoboRobo" not in (served / "CLAUDE.md").read_text()
    assert not (served / "scheduled" / "session.md").exists()
    assert "Sync from RoboRobo: CLAUDE.md, scheduled/session.md" in git(served, "log", "-1", "--format=%s")
    assert run("check", "--repo", served) == 0
    assert git(served, "rev-parse", "HEAD") == git(served, "rev-parse", "origin/main")  # pushed


def test_the_checker_can_have_its_own_command(served, tmp_path):
    from roborobo import night
    prog = config.load(served)
    marker = tmp_path / "who.txt"
    prog.runs["checker"] = ["sh", "-c", f'echo checker > {marker}', "sh"]
    assert night.run_agent(prog, "check it", 1, tmp_path / "c.log", role="checker") is None
    assert marker.read_text() == "checker\n"
    marker.unlink()
    assert night.run_agent(prog, "Run line L0001. write your summary to sessions/x.json "
                           'Set "instruction" in the summary to "none"', 1, tmp_path / "w.log") is None
    assert not marker.exists()  # the worker used the agent command


def test_commands_are_forwarded_only_inside_a_run(served, monkeypatch, capsys):
    monkeypatch.setenv("ROBOROBO_SERVICE_URL", "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.delenv("ROBOROBO_RUN_TOKEN", raising=False)
    assert run("next-id", "E", "--repo", served) == 0  # ran here, not forwarded
    assert capsys.readouterr().out.strip() == "E0001"


def test_lines_changed_since_their_page_was_opened_are_marked(served, tmp_path):
    import re
    db = conn(tmp_path)
    index = dashboard_page(served, tmp_path, db)
    row_sig = re.search(r"<div class='row' data-line='demo/L0001' data-sig='([0-9a-f]{16})'>", index).group(1)
    assert "<span class=new hidden>new</span>" in index and "localStorage.getItem" in index
    line_page = (tmp_path / "state" / "dashboard" / "demo" / "lines" / "L0001.html").read_text()
    assert f'localStorage.setItem("roborobo:demo/L0001", "{row_sig}")' in line_page  # opening it marks it seen
    append(served, "lines.jsonl", line("L0001", "update", state="The heater is ruled out now."))
    commit(served, "news")
    changed = dashboard_page(served, tmp_path, db)
    assert re.search(r"<div class='row' data-line='demo/L0001' data-sig='([0-9a-f]{16})'>", changed).group(1) != row_sig


# The dashboard's actions: forms on the page, sent with the scientist's token.

TOKEN = "the-scientists-dashboard-token"


@pytest.fixture
def acting(served, tmp_path, monkeypatch):
    """The dashboard served with a token; returns a function that posts a form."""
    from urllib.error import HTTPError
    from urllib.parse import parse_qs, urlencode, urlsplit
    from urllib.request import HTTPRedirectHandler, Request, build_opener
    from roborobo import dashboard
    monkeypatch.setenv("ROBOROBO_DASHBOARD_TOKEN", TOKEN)
    conn(tmp_path)  # the service's database exists
    server = dashboard.serve_http(tmp_path / "state", "127.0.0.1", 0, [served])
    url = f"http://127.0.0.1:{server.server_address[1]}/action"

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = build_opener(NoRedirect)

    def post(token=TOKEN, referer="/index.html", **fields):
        """Returns 200 and what the page will say (a done action redirects back
        with it), or the refusal's code and page."""
        data = urlencode({"program": "demo", "token": token, **fields}).encode()
        request = Request(url, data=data, headers={"Referer": f"http://dashboard{referer}"})
        try:
            return 200, opener.open(request).read().decode()
        except HTTPError as error:
            if error.code == 303:
                where = urlsplit(error.headers["Location"])
                assert where.path == referer
                return 200, parse_qs(where.query)["said"][0]
            return error.code, error.read().decode()

    yield post
    server.shutdown()


def test_actions_need_the_dashboard_token(served, acting):
    before = git(served, "rev-parse", "HEAD")
    code, page = acting(token="a guess", action="line-status", line="L0002", status="active")
    assert code == 403 and "that isn&#x27;t the dashboard token" in page
    code, page = acting(token="", action="resume")
    assert code == 403
    assert git(served, "rev-parse", "HEAD") == before


def test_without_a_token_configured_the_page_is_read_only(served, tmp_path, monkeypatch):
    monkeypatch.delenv("ROBOROBO_DASHBOARD_TOKEN", raising=False)
    assert "<form" not in dashboard_page(served, tmp_path, conn(tmp_path))
    from roborobo import actions
    with pytest.raises(actions.Refused, match="read-only"):
        actions.perform(tmp_path / "state", [served], {"token": "x", "action": "resume"})


def test_approve_a_proposed_line_from_the_dashboard(served, tmp_path, acting):
    index = dashboard_page(served, tmp_path, conn(tmp_path))
    page = line_page_of(tmp_path, "L0002")
    assert "<button>Make it active</button>" in page and "<button>Drop it</button>" in page
    assert TOKEN not in page + index  # the token is never written into the page
    code, said = acting(action="line-status", line="L0002", status="active")
    assert code == 200 and "L0002 is made active." in said
    record = json.loads((served / "ledger" / "lines.jsonl").read_text().splitlines()[-1])
    assert (record["line_id"], record["status"], record["by"]) == ("L0002", "active", "human")
    assert "L0002 made active by Pat, from the dashboard" in git(served, "log", "-1", "--format=%s")
    assert git(served, "rev-parse", "HEAD") == git(served, "rev-parse", "origin/main")  # pushed
    assert run("check", "--repo", served) == 0
    code, said = acting(action="line-status", line="L0002", status="dropped")
    assert code == 403 and "L0002 is active; from there it can be made closed or paused" in said



def test_resume_a_paused_line_from_the_dashboard(served, tmp_path, acting):
    db = conn(tmp_path)
    service.pause_line(db, config.load(served), "L0001", "a merge conflicted")
    dashboard_page(served, tmp_path, db)
    assert "<button>Resume L0001</button>" in line_page_of(tmp_path, "L0001")
    code, said = acting(action="resume", line="L0001")
    assert code == 200 and "demo:L0001 is resumed" in said
    assert db.execute("SELECT COUNT(*) FROM paused_lines").fetchone()[0] == 0
    code, said = acting(action="resume", line="L0001")
    assert code == 403 and "isn&#x27;t paused" in said


def test_a_line_page_has_an_instruction_box(served, tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOROBO_DASHBOARD_TOKEN", TOKEN)
    dashboard_page(served, tmp_path, conn(tmp_path))
    page = (tmp_path / "state" / "dashboard" / "demo" / "lines" / "L0001.html").read_text()
    assert "<button>Save</button>" in page and "Saving starts nothing." in page and 'localStorage.getItem("roborobo:token")' in page
    assert "prompt(" not in page and "token-ask" in page  # asked for in the page, not a pop-up


def test_an_approval_during_a_run_doesnt_pause_the_line(served, tmp_path, monkeypatch):
    """Nov 23: Chris approved a program.toml change on main while L0020 ran; its
    worktree didn't have it, the check there failed, and the line was paused."""
    db = conn(tmp_path)
    look(served, db)
    instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n")
    real = service.lines.refresh
    done = []

    def approve_then_refresh(program, line):
        if not done:  # main moves while the run is under way
            done.append(1)
            with (served / "budgets.yaml").open("a") as f:
                f.write("# a limit Pat changed mid-run\n")
            commit(served, "a change by Pat")
            assert run("approve", "--repo", served) == 0
        return real(program, line)

    monkeypatch.setattr(service.lines, "refresh", approve_then_refresh)
    notes = look(served, db)
    (done_run,) = runs(db)
    assert json.loads(done_run["problems"]) == [] and not any("paused" in n for n in notes)
    assert (served / "sessions" / f"{done_run['stem']}.json").exists()  # merged into main


def test_finished_work_left_on_a_line_branch_is_merged(served, tmp_path):
    db = conn(tmp_path)
    look(served, db)
    prog = config.load(served)
    tree, _ = service.lines.start(prog, "L0001")
    (tree.repo / "lines" / "L0001").mkdir(parents=True, exist_ok=True)
    (tree.repo / "lines" / "L0001" / "progress.md").write_text("Work from a run that never merged.\n")
    commit(tree.repo, "L0001 work")
    assert service.lines.unmerged(prog) == ["L0001"]
    notes = look(served, db)
    assert "merged L0001's earlier work into main" in notes
    assert (served / "lines" / "L0001" / "progress.md").exists()
    assert service.lines.unmerged(prog) == []


def test_terms_lines_and_documents_are_linked_where_they_are_mentioned(served, tmp_path):
    """Chris, reading the dashboard: "what's an impulse step method?", "L0009
    should be a link", "if clusterlifter.md is mentioned on the first page,
    link it on the first page"."""
    (served / "lines" / "L0001").mkdir(parents=True)
    (served / "lines" / "L0001" / "design.md").write_text("# Design\n")
    append(served, "terms.jsonl", {"term_id": "T0001", "at": "2026-11-05T23:00:00+00:00",
                                   "by": "agent", "term": "Impulse step",
                                   "definition": "Advancing held spheres by small pushes each step.",
                                   "aliases": ["impulse-step method"]})
    append(served, "lines.jsonl", line(
        "L0001", "update",
        state="The impulse-step method is described in design.md; its loops are in L0003.",
        next="Wait for the Fan line's loops, then compare.",
        closes_when="The drift is explained with the heater off, or ruled out."))
    append(served, "lines.jsonl", line("L0003", "update", parent="L0001"))
    commit(served, "terms and links")
    index = dashboard_page(served, tmp_path, conn(tmp_path))
    assert ("<a class=term href='demo/terms.html#T0001' title='Advancing held spheres by small "
            "pushes each step.'>impulse-step method</a>") in index
    assert "<a class=doc href='demo/docs/lines/L0001/design.md.html' title='lines/L0001/design.md'>design.md</a>" in index
    assert "<a class=line href='demo/lines/L0003.html' title='Fan'>L0003</a>" in index
    assert "<b>Next:</b> Wait for the Fan line" in index
    assert "href='demo/terms.html'>terms</a>" in index
    folder = tmp_path / "state" / "dashboard" / "demo"
    terms = (folder / "terms.html").read_text()
    assert "id='T0001'><b>Impulse step</b>" in terms and "also: impulse-step method" in terms
    line_page = (folder / "lines" / "L0001.html").read_text()
    assert "href='../terms.html#T0001'" in line_page
    assert "<b>Closes when:</b> The drift is explained" in line_page
    assert "branches: <a class=line href='../lines/L0003.html'>L0003 Fan</a>" in line_page
    fan = (folder / "lines" / "L0003.html").read_text()
    assert "from <a class=line href='../lines/L0001.html'>L0001 Heater</a>" in fan


def test_a_lines_name_links_only_as_written(served, tmp_path):
    append(served, "lines.jsonl", line("L0003", "update",
                                       state="The heater was off; see Heater for the rest."))
    commit(served, "a mention")
    index = dashboard_page(served, tmp_path, conn(tmp_path))
    assert "The heater was off; see <a class=line href='demo/lines/L0001.html'" in index


def test_answer_boxes_are_full_width(served, tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOROBO_DASHBOARD_TOKEN", TOKEN)
    dashboard_page(served, tmp_path, conn(tmp_path))
    page = (tmp_path / "state" / "dashboard" / "demo" / "lines" / "L0001.html").read_text()
    assert "<form class='act wide'" in page and "form.act.wide { display: block" in page



def test_close_a_line_from_the_dashboard_with_why(served, tmp_path, acting):
    db = conn(tmp_path)
    look(served, db)
    code, said = acting(action="line-status", line="L0001", status="closed", comment="  ")
    assert code == 403 and "say why" in said
    code, said = acting(action="line-status", line="L0001", status="closed",
                        comment="The phase diagram lines have superseded it.")
    assert code == 200 and "L0001 is closed. Your comment is saved as a note" in said
    update, note = [json.loads(row) for row in
                    (served / "ledger" / "lines.jsonl").read_text().splitlines()[-2:]]
    assert (update["status"], update["by"]) == ("closed", "human")
    assert note["text"] == "Closed by Pat: The phase diagram lines have superseded it."
    assert run("check", "--repo", served) == 0
    index = (tmp_path / "state" / "dashboard" / "index.html").read_text()
    folded = index.split("<details class=inactive>")[1]  # there, behind the toggle
    assert "Show the 1 paused, closed and dropped lines" in folded
    assert "data-line='demo/L0001'" in folded and ">Closed</span>" in folded
    page = (tmp_path / "state" / "dashboard" / "demo" / "lines" / "L0001.html").read_text()
    assert "<h3>This line is closed</h3>" in page and "<button>Reopen L0001</button>" in page
    assert "Closed by Pat: The phase diagram lines have superseded it." in page
    instruct(served, "## L0001 Heater\n\n1. One more thing.\n")
    assert any("L0001 isn't active" in n for n in look(served, db))  # a closed line doesn't run
    code, said = acting(action="line-status", line="L0001", status="active")
    assert code == 200 and "L0001 is made active." in said


def row(index, lid):
    return index.split(f"data-line='demo/{lid}'")[1].split("<!--/row-->")[0]


def line_page_of(tmp_path, lid):
    return (tmp_path / "state" / "dashboard" / "demo" / "lines" / f"{lid}.html").read_text()


def live_index(tmp_path):
    return (tmp_path / "state" / "dashboard" / "index.html").read_text()


def test_answers_wait_for_start_then_run_together(served, tmp_path, acting):
    """Chris, Nov 24: answering several questions may take hours; nothing should
    start until a Start button says "I've done everything I want to do"."""
    from roborobo import actions
    db = conn(tmp_path)
    look(served, db)
    code, said = acting(action="instruct", line="L0001", text="Yes, go higher.",
                        question="Should the heater go higher?")
    assert code == 200 and "Nothing starts yet" in said
    text = (served / "lines" / "L0001" / "instruction.md").read_text()
    assert "> Should the heater go higher?" in text and "Pat, from the dashboard" in text
    assert "Ready when you are" in row(live_index(tmp_path), "L0001")  # on the page at once
    saved = line_page_of(tmp_path, "L0001")
    assert "Also saved for its next run" in saved and "Yes, go higher." in saved
    assert "1 instruction saved. Nothing runs until you press Start." in saved
    look(served, db)
    look(served, db)
    assert runs(db) == []  # saved, not started
    assert "Ready when you are" in row(dashboard_page(served, tmp_path, db), "L0001")
    code, _ = acting(action="instruct", line="L0001", text="And try the spare heater.")
    assert code == 200
    look(served, db)
    assert runs(db) == []
    actions.WAKE.clear()
    code, said = acting(action="start", line="L0001")
    assert code == 200 and "L0001 starts within a minute, with your 2 new instructions." in said
    assert actions.WAKE.is_set()
    assert ">Starting</span>" in row(live_index(tmp_path), "L0001")
    look(served, db)
    (ran,) = runs(db)  # one run for both answers
    assert ran["line"] == "L0001"
    assert "Your instruction is lines/L0001/instruction.md" in prompts(tmp_path)[0]
    after = dashboard_page(served, tmp_path, db)
    assert ">Idle</span>" in row(after, "L0001")
    assert "Also saved for its next run" not in line_page_of(tmp_path, "L0001")  # the run took them
    code, said = acting(action="instruct", line="L0001", text="   ")
    assert code == 403 and "nothing was written" in said


def test_start_runs_a_line_whatever_its_limits_and_without_an_instruction(served, tmp_path, acting):
    toml = served / "program.toml"
    toml.write_text(toml.read_text().replace("min_minutes_between_runs = 0",
                                             "min_minutes_between_runs = 600\n"
                                             "max_runs_per_line_per_day = 1"))
    commit(served, "tight limits")
    assert run("approve", "--repo", served) == 0
    db = conn(tmp_path)
    look(served, db)
    instruct(served, "## L0001 Heater\n\n1. Is it the heater?\n")
    look(served, db)
    assert len(runs(db)) == 1
    code, said = acting(action="start", line="L0001")
    assert code == 200 and "from its progress notes (no new instruction)" in said
    look(served, db)
    assert len(runs(db)) == 2  # despite one run a day and a ten-hour gap
    assert "asked for this run without a new instruction" in prompts(tmp_path)[2]
    code, said = acting(action="start", line="L0002")
    assert code == 403 and "L0002 is proposed; make it active first" in said


def test_a_lines_automatic_runs_wait_while_instructions_are_unstarted(served, tmp_path, acting):
    db = conn(tmp_path)
    look(served, db)
    acting(action="instruct", line="L0001", text="Hold on, I have more to say.")
    look(served, db)
    prog = config.load(served)
    service.set_timer(tmp_path / "state" / "service.db", prog, "L0001",
                      service.now_utc() + timedelta(minutes=11), "collect a job")
    db.execute("UPDATE timers SET due = ?", ((service.now_utc() - timedelta(minutes=1)).isoformat(),))
    db.commit()
    notes = look(served, db)
    assert runs(db) == []
    assert any("L0001 has instructions from Pat that aren't started" in n for n in notes)
    acting(action="start", line="L0001")
    look(served, db)
    assert len(runs(db)) == 1  # one run: the instruction and the timer together


def test_an_instruction_pushed_from_a_discussion_waits_for_start_too(served, tmp_path, acting):
    db = conn(tmp_path)
    look(served, db)
    (served / "lines" / "L0003").mkdir(parents=True)
    (served / "lines" / "L0003" / "instruction.md").write_text(
        "# Instructions for L0003\n\n## 2026-11-24 09:00, Pat, from a discussion\n\n"
        "Try the fan at half speed.\n")
    commit(served, "an instruction from a discussion")
    look(served, db)
    assert runs(db) == []
    index = dashboard_page(served, tmp_path, db)
    assert "Ready when you are" in row(index, "L0003") and "1 instruction saved" in row(index, "L0003")
    assert "Try the fan at half speed." in line_page_of(tmp_path, "L0003")
    acting(action="start", line="L0003")
    look(served, db)
    assert [r["line"] for r in runs(db)] == ["L0003"]


def test_every_line_shows_its_state(served, tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOROBO_DASHBOARD_TOKEN", TOKEN)
    db = conn(tmp_path)
    look(served, db)
    prog = config.load(served)
    index = dashboard_page(served, tmp_path, db)
    assert "<span class='state state-idle'>Idle</span>" in row(index, "L0001")
    assert "<button>" not in index  # Start is on each line's page, in the bar at its foot
    assert "<div class=bar>" in line_page_of(tmp_path, "L0001")
    assert "<button>Start L0001</button>" in line_page_of(tmp_path, "L0001")
    assert "<span class='state state-you'>Proposed by the agent</span>" in row(index, "L0002")
    assert "<button>Start L0002</button>" not in line_page_of(tmp_path, "L0002")
    asked = {"date": "2026-11-17", "line_id": "L0003", "instruction": "none",
             "headline": "A run with a question.", "experiments": [], "claims_changed": [],
             "questions": [{"question": "Should the fan go faster?",
                            "why_it_matters": "It decides the next run.",
                            "default_if_unanswered": "Keep it where it is."}],
             "budget": {}, "open_blockers": [],
             "proposed_directions": [{"direction": "Keep going.", "rationale": "No reason to stop."}]}
    (served / "sessions" / "2026-11-17-L0003.json").write_text(json.dumps(asked))
    commit(served, "a run that asks")
    service.pause_line(db, prog, "L0001", "a merge conflicted")
    index = dashboard_page(served, tmp_path, db)
    assert "<span class='state state-you'>Waiting on you</span>" in row(index, "L0003")
    assert "0 of 1 question answered" in row(index, "L0003")
    assert "<span class='state state-stuck'>Stopped: needs fixing</span>" in row(index, "L0001")
    assert "<button>Start L0001</button>" not in line_page_of(tmp_path, "L0001")
    service.resume(db, "demo:L0001")
    db.execute("INSERT INTO runs (program, line, day, stem, started, reasons) VALUES "
               "('demo', 'L0001', '2026-11-24', '2026-11-24-L0001', ?, '[]')",
               (service.now_utc().isoformat(),))
    db.commit()
    index = dashboard_page(served, tmp_path, db)
    assert "<span class='state state-running'>Running</span>" in row(index, "L0001")
    page = (tmp_path / "state" / "dashboard" / "demo" / "lines" / "L0003.html").read_text()
    assert "state-you" in page and "<button>Start L0003</button>" in page


def test_the_discuss_thread_writes_the_lines_own_instruction(served, tmp_path):
    from urllib.parse import parse_qs, urlparse
    import html
    index = dashboard_page(served, tmp_path, conn(tmp_path))
    link = html.unescape(re.search(r"href='(claude://code/new\?[^']+)'", line_page_of(tmp_path, "L0001")).group(1))
    prompt = parse_qs(urlparse(link).query)["q"][0]
    assert "append it to lines/L0001/instruction.md" in prompt
    assert "give me the `git push` command" in prompt and "press Start on L0001" in prompt
    assert "instructions/" not in prompt


def test_a_line_page_tells_the_lines_story_and_answers_collapse(served, tmp_path, acting):
    """Chris, Nov 24: "I answered the questions but the boxes are all still
    there and my answers are gone", "it should explain clearly what the point
    of the line is", "nothing from the checker that I can see. Why not?",
    "I honestly have no idea if line 9 is going to run now or not"."""
    db = conn(tmp_path)
    append(served, "lines.jsonl", line(
        "L0003", "update", purpose="Find out whether the fan changes the drift, because the "
                                   "heater alone doesn't explain it."))
    asked = {"date": "2026-11-17", "line_id": "L0003", "instruction": "none",
             "headline": "A run with two questions.", "experiments": [], "claims_changed": [],
             "answers": [{"asked": "Does the fan matter?", "status": "partly",
                          "answer": "At full speed, yes."}],
             "questions": [{"question": "Should the fan go faster?",
                            "why_it_matters": "It decides the next run.",
                            "default_if_unanswered": "Keep it where it is."},
                           {"question": "Should the lid stay on?",
                            "why_it_matters": "It changes the airflow.",
                            "default_if_unanswered": "Leave it on."}],
             "budget": {}, "open_blockers": [],
             "proposed_directions": [{"direction": "Keep going.", "rationale": "No reason to stop."}]}
    (served / "sessions" / "2026-11-17-L0003.json").write_text(json.dumps(asked))
    commit(served, "a run that asks, and a purpose")
    index = dashboard_page(served, tmp_path, db)
    assert "Find out whether the fan changes the drift" in row(index, "L0003")
    page = line_page_of(tmp_path, "L0003")
    order = [page.index(mark) for mark in (
        "<h3>What this line is for</h3>", "<h3>Where it stands</h3>", "<h3>Its latest run</h3>",
        "<h3 id=your-turn>Your turn</h3>", "<details class=more>", "<div class=bar>")]
    assert order == sorted(order)  # read top to bottom
    assert "Find out whether the fan changes the drift" in page
    assert "The agent (partly):</span> At full speed, yes." in page  # whose words they are
    assert "Not checked. There is no review of this run." in page
    assert page.count("<button>Save answer</button>") == 2
    assert "0 of 2 questions answered. Unanswered questions take the default shown. " \
           "Nothing runs until you press Start." in page
    assert "The agent hasn't written this yet" in line_page_of(tmp_path, "L0001")  # no purpose

    code, _ = acting(action="instruct", line="L0003", text="Yes, a notch faster.",
                     question="Should the fan go faster?", anchor="your-turn",
                     referer="/demo/lines/L0003.html")
    assert code == 200
    page = line_page_of(tmp_path, "L0003")
    assert "✓ You answered:</span> Yes, a notch faster." in page
    assert "<details><summary>Change your answer</summary>" in page
    assert page.count("placeholder='Your answer.") == 2  # one open, one behind "Change"
    assert "1 of 2 questions answered" in page
    assert "1 of 2 questions answered" in row(live_index(tmp_path), "L0003")
    assert "Waiting on you" in row(live_index(tmp_path), "L0003")
    acting(action="instruct", line="L0003", text="On.", question="Should the lid stay on?")
    assert "Ready when you are" in row(live_index(tmp_path), "L0003")
    assert "2 of 2 questions answered. Nothing runs until you press Start." in \
        line_page_of(tmp_path, "L0003")
    look(served, db)
    assert runs(db) == []  # still nothing runs until Start



def test_a_line_keeps_pinned_items_with_their_earlier_versions(served, tmp_path, acting):
    """Chris, Nov 25: "on L0009 the thing that keeps changing is the algorithm.
    So there should be an artifact that is the pseudocode for the current
    algorithm. It should have a previous/next so I can see how it evolves."""
    db = conn(tmp_path)
    code, said = acting(action="pin", line="L0001", title="Current algorithm (pseudocode)",
                        about="Every step of the heater test, in order.")
    assert code == 200 and "is pinned to L0001. The line makes it at its next run" in said
    record = json.loads((served / "ledger" / "lines.jsonl").read_text().splitlines()[-1])
    assert record["by"] == "human" and record["pinned"] == [
        {"slug": "current-algorithm-pseudocode", "title": "Current algorithm (pseudocode)",
         "about": "Every step of the heater test, in order."}]
    assert run("check", "--repo", served) == 0
    page = line_page_of(tmp_path, "L0001")
    assert "<h3>Always on this line</h3>" in page and "Not made yet" in page
    assert page.index("<h3>Always on this line</h3>") < page.index("<h3>Its latest run</h3>")
    assert "<button>Unpin Current algorithm (pseudocode)</button>" in page
    code, said = acting(action="pin", line="L0001", title="???", about="x")
    assert code == 403

    pinned = served / "lines" / "L0001" / "pinned"
    pinned.mkdir(parents=True)
    for text in ("1. Heat.\n", "1. Heat.\n2. Wait.\n", "1. Heat slowly.\n2. Wait.\n"):
        (pinned / "current-algorithm-pseudocode.md").write_text("# Algorithm\n\n" + text)
        commit(served, f"algorithm: {text.splitlines()[-1]}")
    append(served, "lines.jsonl", line("L0001", "update", pinned=[
        {"slug": "drift-plot", "title": "Drift plot", "about": "Drift against time."}]))
    (pinned / "drift-plot.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    commit(served, "a plot, pinned by the agent")
    dashboard_page(served, tmp_path, db)
    page = line_page_of(tmp_path, "L0001")
    assert "version 3 of 3" in page and "Heat slowly." in page and "Not made yet" not in page
    assert "<a href='../pins/L0001/current-algorithm-pseudocode/2.html'>← previous version</a>" in page
    assert "<img class=pin-img src='../pins/L0001/drift-plot/1.svg'" in page
    folder = tmp_path / "state" / "dashboard" / "demo" / "pins" / "L0001"
    first = (folder / "current-algorithm-pseudocode" / "1.html").read_text()
    second = (folder / "current-algorithm-pseudocode" / "2.html").read_text()
    assert "<b>Version 1 of 3</b>" in first and "<li>Heat.</li>" in first and "Wait." not in first
    assert "<span class=none>← earlier</span>" in first and "<a href='2.html'>later →</a>" in first
    assert "<a href='1.html'>← earlier</a>" in second and "<a href='3.html'>later →</a>" in second
    assert "What changed: algorithm: 2. Wait." in second
    assert (folder / "drift-plot" / "1.svg").read_text().startswith("<svg")

    (pinned / "current-algorithm-pseudocode.md").write_text("# Algorithm\n\n1. Heat.\n2. Cool.\n")
    commit(served, "algorithm: cool at the end")
    dashboard_page(served, tmp_path, db)
    third = (folder / "current-algorithm-pseudocode" / "3.html").read_text()
    assert "<b>Version 3 of 4</b>" in third and "<a href='4.html'>later →</a>" in third

    code, said = acting(action="unpin", line="L0001", slug="drift-plot")
    assert code == 200 and "no longer pinned" in said
    assert "Drift plot" not in line_page_of(tmp_path, "L0001")
    assert (pinned / "drift-plot.svg").exists()  # unpinning deletes nothing
    assert run("check", "--repo", served) == 0
