"""The service's dashboard: what needs the scientist, then each line.

``program serve`` writes ``<state>/dashboard/index.html`` after every look,
and each program's map beside it (``<state>/dashboard/<program>/map.html``),
and serves the folder over HTTP. The page is built from the records and the
service's database; no prose is written here.

Top of the page, per program, **needs you**: the program paused (and the
command to resume it), lines the agent proposed, questions from the latest
run of each line, open challenges, failed or vanished jobs, and lines held
back by the service's limits. Then **lines**: one row per active line with
where it stands, its last run, the checker's verdicts on it, jobs in flight
and pending events, and a Discuss link that opens a Claude Code thread on
that line in the scientist's clone.
"""

from __future__ import annotations

import html
import json
import sqlite3
import threading
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from roborobo import cluster, mapview
from roborobo.config import Program
from roborobo.gitutil import committed_at
from roborobo.mapview import fold_lines
from roborobo.records import documents, load_ledger

STYLE = """
:root { --bg:#f3f6f8; --surface:#fff; --fg:#16212b; --muted:#586674; --line:#d3dbe2;
  --accent:#1d5f8a; --good:#2b7a58; --warn:#a86a12; --bad:#b03f38;
  font-family: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif; }
@media (prefers-color-scheme: dark) { :root { --bg:#10151a; --surface:#182029; --fg:#e3e9ee;
  --muted:#9aa8b5; --line:#2d3945; --accent:#74b6e0; --good:#5fc59a; --warn:#e2ad52;
  --bad:#e88077; color-scheme: dark; } }
body { background: var(--bg); color: var(--fg); margin: 0 auto; max-width: 1100px;
  padding: 16px; line-height: 1.45; }
h1 { font-size: 1.4rem; margin: 0 0 4px; } h2 { font-size: 1.15rem; margin: 28px 0 8px; }
h3 { font-size: .95rem; margin: 18px 0 6px; color: var(--muted); text-transform: uppercase;
  letter-spacing: .04em; }
.meta { color: var(--muted); font-size: .9rem; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 14px; margin: 6px 0; }
.needs .card { border-left: 4px solid var(--warn); }
.needs .bad { border-left-color: var(--bad); }
table { width: 100%; border-collapse: collapse; background: var(--surface);
  border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; vertical-align: top; padding: 8px 10px;
  border-bottom: 1px solid var(--line); font-size: .92rem; }
th { color: var(--muted); font-weight: 500; }
code { font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace; font-size: .85em; }
a { color: var(--accent); }
.good { color: var(--good); } .warn { color: var(--warn); } .bad { color: var(--bad); }
.none { color: var(--muted); }
@media (max-width: 700px) { td:nth-child(3), th:nth-child(3) { display: none; } }
"""


def esc(text) -> str:
    return html.escape(str(text))


def discuss_link(program: Program, lid: str, name: str, latest: str | None,
                 reviewed: bool = True) -> str:
    """A claude:// link that opens a Code tab session in the scientist's clone
    (the same path as the service's, which mirrors the Mac's layout)."""
    reads = [f"lines/{lid}/progress.md"]
    if latest:
        reads.append(f"sessions/{latest}.json")
        if reviewed:
            reads.append(f"reviews/{latest}.json")
    prompt = (f"Run `git pull` first. Then let's discuss line {lid} ({name}) of the "
              f"{program.name} research program. Read {', '.join(reads)} and wait for my "
              "questions. If we settle on an instruction, add it under `## " + lid + "` in "
              "today's file in instructions/ and stop, so I can review and push it.")
    return (f"claude://code/new?folder={quote(str(program.repo))}&q={quote(prompt)}")


def latest_documents(program: Program) -> tuple[dict, dict]:
    """The newest session summary and review per line, by file name."""
    newest = {}
    for folder, schema in (("sessions", "session-summary.schema.json"),
                           ("reviews", "review.schema.json")):
        found, _ = documents(program, folder, schema)
        by_line: dict[str, tuple[str, dict]] = {}
        for rel, doc in sorted(found, key=lambda item: run_order(item[0])):
            if doc.get("line_id"):
                by_line[doc["line_id"]] = (rel, doc)
        newest[folder] = by_line
    return newest["sessions"], newest["reviews"]


def run_order(rel: str) -> tuple[str, int]:
    """sessions/2026-11-23-L0013-2.json sorts after sessions/2026-11-23-L0013.json."""
    stem = Path(rel).stem
    parts = stem.split("-")
    number = int(parts[-1]) if len(parts) == 5 and parts[-1].isdigit() else 1
    return ("-".join(parts[:3]), number)


def answered_after(program: Program) -> int:
    """When the newest instruction file was committed: questions from runs
    committed before it are taken as answered by it."""
    files = sorted((program.repo / "instructions").glob("20*.md"))
    return committed_at(program.repo, files[-1].relative_to(program.repo).as_posix()) if files else 0


def program_section(conn: sqlite3.Connection, program: Program, notes: list[str]) -> str:
    records, _ = load_ledger(program)
    lines = fold_lines(records.get("lines.jsonl", []))
    sessions, reviews = latest_documents(program)
    open_jobs = [j for j in cluster.load_jobs(program) if not j.get("ended_at")]
    paused = conn.execute("SELECT at, reason FROM paused WHERE program = ?",
                          (program.name,)).fetchone()
    pending = conn.execute("SELECT line, detail FROM events WHERE program = ? AND "
                           "status = 'pending' ORDER BY id", (program.name,)).fetchall()
    runs = conn.execute("SELECT * FROM runs WHERE program = ? ORDER BY id DESC",
                        (program.name,)).fetchall()

    needs = []
    if paused:
        needs.append(("bad", f"<b>Paused</b> since {esc(paused['at'][:16])}: "
                             f"{esc(paused['reason'])}<br><span class=meta>Fix it in the "
                             f"service's clone, then <code>program serve --resume "
                             f"{esc(program.name)}</code></span>"))
    for lid, line in sorted(lines.items()):
        if line.get("status") == "proposed":
            needs.append(("", f"<b>{esc(lid)} proposed</b> by the agent: "
                              f"{esc(line.get('name', ''))}. {esc(line.get('reason', ''))}"
                              "<br><span class=meta>Make it active (a line update by you) or "
                              "drop it.</span>"))
    answered = answered_after(program)
    for lid, (rel, doc) in sorted(sessions.items()):
        if committed_at(program.repo, rel) < answered:
            continue  # asked before the newest instruction, which answers it
        for question in doc.get("questions", []):
            needs.append(("", f"<b>{esc(lid)} asks</b> ({esc(rel)}): "
                              f"{esc(question['question'])}<br><span class=meta>If "
                              f"unanswered: {esc(question['default_if_unanswered'])}</span>"))
    for lid, (rel, review) in sorted(reviews.items()):
        doubtful = [r for r in review.get("reviews", []) if r["verdict"] in ("doubtful", "broken")]
        if doubtful:
            needs.append(("bad", f"<b>{esc(lid)}: the checker doubts "
                                 f"{len(doubtful)} claim(s)</b> ({esc(rel)}): "
                                 + "; ".join(esc(r["plain_claim"]) for r in doubtful)))
    for note in notes:
        if note.startswith(("job problem:", "waiting:", "error")):
            needs.append(("bad" if note.startswith(("job", "error")) else "", esc(note)))

    out = [f"<h2>{esc(program.name)}</h2>",
           f"<p class=meta>{esc(program.title)} · <a href='{esc(program.name)}/map.html'>"
           "program map</a></p>", "<h3>Needs you</h3>", "<div class=needs>"]
    out += [f"<div class='card {kind}'>{text}</div>" for kind, text in needs] or [
        "<div class=card style='border-left-color:var(--good)'>Nothing.</div>"]
    out.append("</div><h3>Lines</h3><table><tr><th>Line</th><th>Where it stands</th>"
               "<th>Last run</th><th>Checker</th><th>In flight</th><th></th></tr>")
    for lid, line in sorted(lines.items()):
        if line.get("status") != "active":
            continue
        last = next((r for r in runs if r["line"] == lid), None)
        session = sessions.get(lid)
        review = reviews.get(lid)
        stem = Path(session[0]).stem if session else None
        if last:
            problems = json.loads(last["problems"] or "[]")
            run_text = (f"{esc(last['stem'])}<br><span class=meta>"
                        f"{esc((last['ended'] or 'running')[:16])}</span>"
                        + (f"<br><span class=bad>{esc('; '.join(problems))}</span>"
                           if problems else ""))
        elif session:
            run_text = f"{esc(stem)}<br><span class=meta>before the service</span>"
        else:
            run_text = "<span class=none>none yet</span>"
        if session:
            run_text += f"<br>{esc(session[1].get('headline', ''))}"
        if review:
            verdicts = [r["verdict"] for r in review[1].get("reviews", [])]
            counts = {v: verdicts.count(v) for v in ("holds", "doubtful", "broken")}
            check = " ".join(f"<span class={'good' if v == 'holds' else 'bad'}>{n} {v}</span>"
                             for v, n in counts.items() if n) or "<span class=none>no claims</span>"
        else:
            check = "<span class=none>not checked</span>"
        jobs = [j for j in open_jobs if j.get("line_id") == lid]
        events = [p for p in pending if p["line"] == lid]
        flight = []
        if jobs:
            flight.append(f"{len(jobs)} job(s) on {esc(', '.join(sorted({j['host'] for j in jobs})))}")
        flight += [f"waiting: {esc(e['detail'])}" for e in events]
        reviewed = bool(review) and Path(review[0]).stem == stem
        link = discuss_link(program, lid, line.get("name", lid), stem, reviewed)
        out.append(f"<tr><td><b>{esc(lid)}</b><br>{esc(line.get('name', ''))}</td>"
                   f"<td>{esc(mapview.short(line.get('state', ''), 220))}</td>"
                   f"<td>{run_text}</td><td>{check}</td>"
                   f"<td>{'<br>'.join(flight) or '<span class=none>nothing</span>'}</td>"
                   f"<td><a href='{esc(link)}'>Discuss</a></td></tr>")
    out.append("</table>")
    return "\n".join(out)


def write(state: Path, conn: sqlite3.Connection, programs: list[Program],
          looks: dict[str, list[str]], errors: dict[str, list[str]]) -> Path:
    """Write the dashboard and each program's map; return the page's path."""
    folder = state / "dashboard"
    folder.mkdir(parents=True, exist_ok=True)
    sections = []
    for program in programs:
        mapview.write(program, folder / program.name / "map.html", 14)
        sections.append(program_section(conn, program, looks.get(program.name, [])))
    for name, lines in errors.items():
        sections.append(f"<h2>{esc(name)}</h2><div class=needs><div class='card bad'>"
                        f"<b>The last look failed</b> (tried again at the next):"
                        f"<pre>{esc(chr(10).join(lines))}</pre></div></div>")
    page = ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            "<meta http-equiv=refresh content=60><title>RoboRobo service</title>"
            f"<style>{STYLE}</style></head><body><h1>RoboRobo service</h1>"
            f"<p class=meta>Last look {datetime.now().astimezone():%b %-d, %H:%M %Z}. "
            "This page refreshes every minute. Discuss opens a Claude Code thread in your "
            "clone.</p>" + "\n".join(sections) + "</body></html>")
    path = folder / "index.html"
    path.write_text(page)
    return path


class QuietHandler(SimpleHTTPRequestHandler):
    """No line in the service's log for every page view."""

    def log_message(self, format, *args):  # noqa: A002 (the base class's name)
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # always the latest look
        super().end_headers()


def serve_http(state: Path, host: str, port: int) -> ThreadingHTTPServer:
    """Serve the dashboard folder in a background thread."""
    folder = state / "dashboard"
    folder.mkdir(parents=True, exist_ok=True)
    handler = partial(QuietHandler, directory=str(folder))
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
