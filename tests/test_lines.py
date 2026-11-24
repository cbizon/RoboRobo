"""Branch per line: each line runs in a worktree of its own, lines run in
parallel, and the service merges each clean run into main. Real git
worktrees and the stand-in agent from test_service."""

import json

import pytest

from test_agent_pods import post
from test_service import commit, conn, git, instruct, isolated_git, look, program, run, runs, served  # noqa: F401

from roborobo import config, lines, records, service_api
from roborobo.checks import check_program
from roborobo.render import template_drift


@pytest.fixture
def parallel(served):
    """The served program with two lines allowed at once."""
    toml = served / "program.toml"
    toml.write_text(toml.read_text().replace("min_minutes_between_runs = 0",
                                             "min_minutes_between_runs = 0\nmax_parallel_lines = 2"))
    commit(served, "two lines at once")
    assert run("approve", "--repo", served) == 0
    git(served, "push", "-q", "origin", "main")
    return served


def both_lines(program):
    return instruct(program, "## L0001 Heater\n\n1. Is it the heater?\n\n"
                             "## L0003 Fan\n\n1. Does the fan matter?\n")


def test_two_lines_run_at_once_and_both_merge(parallel, tmp_path, monkeypatch):
    db = conn(tmp_path)
    look(parallel, db)
    monkeypatch.setenv("STAND_IN_MODE", "note")
    day = both_lines(parallel)
    look(parallel, db)
    assert sorted(r["line"] for r in runs(db)) == ["L0001", "L0003"]
    for lid in ("L0001", "L0003"):  # each ran in its own worktree, and was merged
        assert (parallel.parent / f"demo.{lid}" / ".git").exists()
        assert (parallel / "sessions" / f"{day}-{lid}.json").exists()
    notes = [json.loads(line) for line in (parallel / "ledger" / "lines.jsonl").read_text()
             .splitlines() if '"note"' in line]
    assert sorted(n["line_id"] for n in notes) == ["L0001", "L0003"]  # union kept both
    assert check_program(config.load(parallel))[0] == []  # first-parent append-only holds
    assert git(parallel, "rev-parse", "HEAD") == git(parallel, "rev-parse", "origin/main")


def test_two_lines_each_add_a_section_to_program_md(parallel, tmp_path, monkeypatch):
    """Nov 24: L0009 was paused because it and another line both added to
    PROGRAM.md. Each line's section is kept."""
    db = conn(tmp_path)
    look(parallel, db)
    monkeypatch.setenv("STAND_IN_MODE", "section")
    both_lines(parallel)
    notes = look(parallel, db)
    assert not [n for n in notes if n.startswith("paused")]
    text = (parallel / "PROGRAM.md").read_text()
    assert "Where L0001 stands" in text and "Where L0003 stands" in text
    assert "<<<<<<<" not in text
    assert git(parallel, "rev-parse", "HEAD") == git(parallel, "rev-parse", "origin/main")


def test_a_conflict_pauses_only_that_line(parallel, tmp_path, monkeypatch):
    db = conn(tmp_path)
    look(parallel, db)
    monkeypatch.setenv("STAND_IN_MODE", "same-file")
    both_lines(parallel)
    notes = look(parallel, db)
    paused = [n for n in notes if n.startswith("paused L")]
    assert len(paused) == 1 and "conflicts" in paused[0]
    merged = "L0003" if "L0001" in paused[0] else "L0001"
    assert f"as {merged} sees it" in (parallel / "method.md").read_text()
    assert lines.dirty(parallel) == []  # the failed merge was aborted
    assert not db.execute("SELECT * FROM paused").fetchall()  # the program goes on


def test_a_merge_that_breaks_main_is_undone(parallel, tmp_path, monkeypatch):
    """Both lines register E0001 on their own branches: each branch is valid,
    the two together aren't."""
    db = conn(tmp_path)
    look(parallel, db)
    monkeypatch.setenv("STAND_IN_MODE", "register")
    both_lines(parallel)
    notes = look(parallel, db)
    paused = [n for n in notes if n.startswith("paused L")]
    assert len(paused) == 1 and "fails program check, so the merge was undone" in paused[0]
    registered = (parallel / "ledger" / "experiments.jsonl").read_text().count('"E0001"')
    assert registered == 1
    assert check_program(config.load(parallel))[0] == []


def test_lines_get_ids_that_never_collide(parallel, tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOROBO_IDS", str(tmp_path / "ids.db"))
    first = config.load(lines.worktree(config.load(parallel), "L0001").repo)
    second = config.load(lines.worktree(config.load(parallel), "L0003").repo)
    taken = [records.next_id(p, "E") for p in (first, second, first, second)]
    assert taken == ["E0001", "E0002", "E0003", "E0004"]


def test_a_worktree_renders_like_main(parallel):
    tree = lines.worktree(config.load(parallel), "L0001")
    assert tree.repo != parallel and tree.home == parallel.resolve()
    assert template_drift(tree) == []


def test_a_run_cannot_send_commands_for_another_lines_folder(parallel):
    one = lines.worktree(config.load(parallel), "L0001")
    other = lines.worktree(config.load(parallel), "L0003")
    server = service_api.serve("127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    service_api.ACTIVE.add("token-one", one.repo)
    try:
        code, why = post(url, {"argv": ["jobs"], "repo": str(other.repo), "token": "token-one"})
        assert code == 403 and "isn't this run's folder" in why
        code, _ = post(url, {"argv": ["jobs"], "repo": str(one.repo), "token": "token-one"})
        assert code == 200
    finally:
        service_api.ACTIVE.remove("token-one")
        server.shutdown()
