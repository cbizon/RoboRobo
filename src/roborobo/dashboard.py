"""The service's dashboard: what needs the scientist, then each line.

``program serve`` writes ``<state>/dashboard/index.html`` after every look,
and each program's map beside it (``<state>/dashboard/<program>/map.html``),
and serves the folder over HTTP. The page is built from the records and the
service's database; no prose is written here.

Top of the page, per program, **needs you**: the program paused (and the
command to resume it), lines the agent proposed, questions from the latest
run of each line, open challenges, failed or vanished jobs, and lines held
back by the service's limits. Then **lines**: one row per active line with
where it stands (in full, folded), when it was last heard from, its last
run, the checker's verdicts, jobs in flight and pending events, and a
Discuss link that opens a Claude Code thread on that line in the
scientist's clone.

Each line also gets a page (``<program>/lines/<line>.html``): its question,
where it stands, its progress notes, the latest run's answers and questions,
the checker's review, recent runs and notes, and its documents. Every
document a line keeps (files in ``lines/<line>/`` and files its records
point to) gets a page of its own (``<program>/docs/<path>.html``): Markdown
rendered, JSON laid out, anything else as text.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo

import markdown

from roborobo import actions, cluster, mapview
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
form.act { display: inline-block; margin: 6px 8px 0 0; }
form.act textarea { display: block; width: min(100%, 640px); min-height: 60px; margin-bottom: 4px;
  font: inherit; }
form.act button { font: inherit; padding: 3px 12px; border-radius: 6px;
  border: 1px solid var(--accent); background: var(--surface); color: var(--accent); cursor: pointer; }
.new { background: var(--accent); color: var(--surface); border-radius: 4px; padding: 0 5px;
  font-size: .75rem; margin-left: 4px; }
details summary { cursor: pointer; } details[open] summary { color: var(--muted); }
.doc { background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
  padding: 4px 18px; overflow-x: auto; }
.doc table { width: auto; } pre { white-space: pre-wrap; word-break: break-word; }
@media (max-width: 700px) { td:nth-child(4), th:nth-child(4) { display: none; } }
"""
QUIET_DAYS = 3        # an active line not heard from in longer is marked
MAX_DOC_BYTES = 2_000_000


def esc(text) -> str:
    return html.escape(str(text))


def signature(line: dict, summary: str | None, review: str | None) -> str:
    """Changes whenever a line has something new to read: its state, its
    latest summary or review, or a note."""
    seen = json.dumps([line.get("state"), line.get("status"), summary, review,
                       len(line.get("notes", []))])
    return hashlib.sha256(seen.encode()).hexdigest()[:16]


# Marks lines changed since their page was last opened in this browser. What
# was seen is kept in the browser only (localStorage); without it, nothing is
# marked and the page reads the same.
MARK_NEW = """<script>
try {
  document.querySelectorAll("tr[data-line]").forEach(function (row) {
    if (localStorage.getItem("roborobo:" + row.dataset.line) !== row.dataset.sig) {
      row.querySelector(".new").hidden = false;
    }
  });
} catch (e) {}
</script>"""


def mark_seen(key: str, sig: str) -> str:
    return ("<script>try { localStorage.setItem(" + json.dumps("roborobo:" + key) + ", "
            + json.dumps(sig) + "); } catch (e) {}</script>")


def form(program: Program, action: str, button: str, line: str = "", fields: str = "",
         **hidden: str) -> str:
    """A form for one dashboard action, or nothing when the page is read-only
    (no dashboard token configured). The token is filled in by the page's
    script from the browser, never written into the page."""
    if not actions.token():
        return ""
    values = {"action": action, "program": program.name, "line": line, "token": "", **hidden}
    inputs = "".join(f"<input type=hidden name={esc(k)} value='{esc(v)}'>"
                     for k, v in values.items())
    return (f"<form class=act method=post action='/action'>{inputs}{fields}"
            f"<button>{esc(button)}</button></form>")


# Fills the token into a form when it is sent. The token is asked for once and
# kept in this browser; "forget" removes it.
ACT = """<script>
document.querySelectorAll("form.act").forEach(function (f) {
  f.addEventListener("submit", function (e) {
    var t = null;
    try { t = localStorage.getItem("roborobo:token"); } catch (x) {}
    if (!t) {
      t = prompt("Your dashboard token (kept in this browser):");
      if (!t) { e.preventDefault(); return; }
      try { localStorage.setItem("roborobo:token", t.trim()); } catch (x) {}
    }
    f.querySelector("[name=token]").value = t.trim();
  });
});
</script>"""


def page(title: str, body: str, refresh: bool = False) -> str:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            + ("<meta http-equiv=refresh content=60>" if refresh else "")
            + f"<title>{esc(title)}</title><style>{STYLE}</style></head><body>{body}"
            + (ACT if "<form class=act" in body else "") + "</body></html>")


LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def separate_lists(text: str) -> str:
    """A blank line before a list that follows a paragraph line. Most Markdown
    (and the agent's) starts a list without one; Python-Markdown needs it."""
    out, fenced, previous = [], False, ""
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif (not fenced and LIST_ITEM.match(line) and previous.strip()
              and not LIST_ITEM.match(previous) and not previous.startswith((" ", "\t"))):
            out.append("")
        out.append(line)
        previous = line
    return "\n".join(out)


def render_markdown(text: str) -> str:
    """Markdown as HTML, with any raw HTML in it shown as text, not run."""
    text = separate_lists(text)
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists"])
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    return md.convert(text)


def render_file(path: Path) -> str:
    """A document's contents as HTML: Markdown rendered, JSON laid out, other
    text as is."""
    if path.stat().st_size > MAX_DOC_BYTES:
        return f"<p class=none>{path.stat().st_size // 1_000_000} MB: too big to show here.</p>"
    raw = path.read_bytes()
    try:
        text = raw.decode()
    except UnicodeDecodeError:
        return "<p class=none>Not a text file.</p>"
    if path.suffix == ".md":
        return render_markdown(text)
    if path.suffix == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    return f"<pre>{esc(text)}</pre>"


def line_documents(program: Program, lid: str, line: dict) -> list[tuple[str, str]]:
    """(path, kind) for every file the line keeps or its records point to.
    Code branches and other things that aren't files come back with kind and
    no page."""
    found: dict[str, str] = {}
    folder = program.repo / "lines" / lid
    if folder.is_dir():
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                found[path.relative_to(program.repo).as_posix()] = (
                    "progress" if path.name == "progress.md" else "document")
    for kind, rel in line.get("arts", []):
        found.setdefault(rel, kind)
    return list(found.items())


def is_repo_file(program: Program, rel: str) -> bool:
    path = (program.repo / rel).resolve()
    return path.is_relative_to(program.repo.resolve()) and path.is_file()


def local(program: Program, iso: str | None) -> str:
    """A stored time, in the program's timezone."""
    if not iso:
        return "running"
    return f"{datetime.fromisoformat(iso).astimezone(ZoneInfo(program.timezone)):%b %-d %H:%M}"


def when(epoch: float, zone: str = "UTC") -> str:
    moment = datetime.fromtimestamp(epoch, timezone.utc).astimezone(ZoneInfo(zone))
    days = (datetime.now(timezone.utc) - moment).days
    ago = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
    return f"{moment:%b %-d} ({ago})"


def last_heard(program: Program, lid: str, line: dict, summaries: list[tuple[str, dict]],
               runs: list) -> float:
    """When the line last said anything: its newest summary, note or service run."""
    moments = [committed_at(program.repo, rel) for rel, _ in summaries[-1:]]
    moments += [datetime.fromisoformat(n["at"]).timestamp() for n in line.get("notes", [])[-1:]]
    moments += [datetime.fromisoformat(r["started"]).timestamp() for r in runs
                if r["line"] == lid][:1]
    return max(moments, default=0)


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


def line_documents_by_kind(program: Program) -> tuple[dict, dict]:
    """Every session summary and review per line, oldest first by file name."""
    out = {}
    for folder, schema in (("sessions", "session-summary.schema.json"),
                           ("reviews", "review.schema.json")):
        found, _ = documents(program, folder, schema)
        by_line: dict[str, list[tuple[str, dict]]] = {}
        for rel, doc in sorted(found, key=lambda item: run_order(item[0])):
            if doc.get("line_id"):
                by_line.setdefault(doc["line_id"], []).append((rel, doc))
        out[folder] = by_line
    return out["sessions"], out["reviews"]


def latest_documents(program: Program) -> tuple[dict, dict]:
    """The newest session summary and review per line, by file name."""
    sessions, reviews = line_documents_by_kind(program)
    return ({lid: docs[-1] for lid, docs in sessions.items()},
            {lid: docs[-1] for lid, docs in reviews.items()})


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
    all_sessions, _ = line_documents_by_kind(program)
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
        needs.append(("bad", f"<b>Paused</b> since {esc(local(program, paused['at']))}: "
                             f"{esc(paused['reason'])}<br><span class=meta>Fix it in the "
                             f"service's clone, then <code>program serve --resume "
                             f"{esc(program.name)}</code></span>"
                             + form(program, "resume", "Resume")))
    for held in conn.execute("SELECT line, at, reason FROM paused_lines WHERE program = ? "
                             "ORDER BY line", (program.name,)):
        needs.append(("bad", f"<b>{esc(held['line'])} paused</b> since {esc(held['at'][:16])}: "
                             f"{esc(held['reason'])}<br><span class=meta>Fix it in the line's "
                             f"worktree, then <code>program serve --resume "
                             f"{esc(program.name)}:{esc(held['line'])}</code></span>"
                             + form(program, "resume", f"Resume {held['line']}", held["line"])))
    for lid, line in sorted(lines.items()):
        if line.get("status") == "proposed":
            needs.append(("", f"<b>{esc(lid)} proposed</b> by the agent: "
                              f"{esc(line.get('name', ''))}. {esc(line.get('reason', ''))}"
                              "<br><span class=meta>Make it active (a line update by you) or "
                              "drop it.</span><br>"
                              + form(program, "line-status", "Make it active", lid, status="active")
                              + form(program, "line-status", "Drop it", lid, status="dropped")))
    answered = answered_after(program)
    for lid, (rel, doc) in sorted(sessions.items()):
        if committed_at(program.repo, rel) < answered:
            continue  # asked before the newest instruction, which answers it
        for question in doc.get("questions", []):
            needs.append(("", f"<b>{esc(lid)} asks</b> ({esc(rel)}): "
                              f"{esc(question['question'])}<br><span class=meta>If "
                              f"unanswered: {esc(question['default_if_unanswered'])}</span>"
                              + form(program, "instruct", f"Answer {lid}", lid,
                                     "<textarea name=text required placeholder='Your answer; it "
                                     "goes into the line&#39;s instruction and starts the line'>"
                                     "</textarea>", question=question["question"])))
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
               "<th>Last heard</th><th>Last run</th><th>Checker</th><th>In flight</th>"
               "<th></th></tr>")
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
                        f"{esc(local(program, last['ended']))}</span>"
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
        heard = last_heard(program, lid, line, all_sessions.get(lid, []), runs)
        quiet = heard and (datetime.now(timezone.utc).timestamp() - heard) > QUIET_DAYS * 86400
        heard_text = (f"<span class={'warn' if quiet else 'meta'}>{esc(when(heard, program.timezone))}</span>"
                      if heard else "<span class=none>never</span>")
        state = line.get("state", "")
        stands = (f"<details><summary>{esc(mapview.short(state, 160))}</summary>{esc(state)}"
                  "</details>" if len(state) > 160 else esc(state))
        sig = signature(line, session[0] if session else None, review[0] if review else None)
        out.append(f"<tr data-line='{esc(program.name)}/{esc(lid)}' data-sig='{sig}'>"
                   f"<td><a href='{esc(program.name)}/lines/{esc(lid)}.html'><b>{esc(lid)}"
                   f"</b></a><span class=new hidden>new</span><br>{esc(line.get('name', ''))}</td>"
                   f"<td>{stands}</td><td>{heard_text}</td>"
                   f"<td>{run_text}</td><td>{check}</td>"
                   f"<td>{'<br>'.join(flight) or '<span class=none>nothing</span>'}</td>"
                   f"<td><a href='{esc(link)}'>Discuss</a></td></tr>")
    out.append("</table>")
    out.append(MARK_NEW)
    return "\n".join(out)


def line_page(conn: sqlite3.Connection, program: Program, lid: str, line: dict,
              summaries: list[tuple[str, dict]], reviews: list[tuple[str, dict]],
              notes: list[str]) -> str:
    """Everything about one line on one page."""
    runs = conn.execute("SELECT * FROM runs WHERE program = ? AND line = ? ORDER BY id DESC",
                        (program.name, lid)).fetchall()
    open_jobs = [j for j in cluster.load_jobs(program)
                 if not j.get("ended_at") and j.get("line_id") == lid]
    heard = last_heard(program, lid, line, summaries, runs)
    latest = summaries[-1] if summaries else None
    stem = Path(latest[0]).stem if latest else None
    review = next((r for r in reversed(reviews) if Path(r[0]).stem == stem), None)
    sig = signature(line, latest[0] if latest else None, reviews[-1][0] if reviews else None)
    out = [mark_seen(f"{program.name}/{lid}", sig),
           f"<p class=meta><a href='../../index.html'>← all lines</a></p>",
           f"<h1>{esc(lid)} · {esc(line.get('name', ''))}</h1>",
           f"<p class=meta>{esc(line.get('status', ''))} · last heard "
           f"{esc(when(heard, program.timezone)) if heard else 'never'} · <a href='"
           f"{esc(discuss_link(program, lid, line.get('name', lid), stem, bool(review)))}'>"
           "Discuss</a></p>"]
    if line.get("question"):
        source = f" <span class=meta>({esc(line['question_source'])})</span>" \
            if line.get("question_source") else ""
        out += ["<h3>Question</h3>", f"<p>{esc(line['question'])}{source}</p>"]
    out += ["<h3>Where it stands</h3>", f"<p>{esc(line.get('state', '')) or '<span class=none>not recorded</span>'}</p>"]
    progress = program.repo / "lines" / lid / "progress.md"
    if progress.is_file():
        out += ["<h3>Progress notes</h3>", f"<div class=doc>{render_file(progress)}</div>"]
    if open_jobs:
        out += ["<h3>Jobs in flight</h3><ul>"] + [
            f"<li>{esc(j['experiment_id'])}: job {esc(j['job_id'])} on {esc(j['host'])}, "
            f"{esc(j.get('state', ''))} {esc(j.get('elapsed', ''))}</li>" for j in open_jobs] + ["</ul>"]
    if latest:
        rel, doc = latest
        out += [f"<h3>Latest run</h3><p><b>{esc(doc.get('headline', ''))}</b> "
                f"<span class=meta>({esc(rel)})</span></p>"]
        for answer in doc.get("answers", []):
            out.append(f"<div class=card><b>Asked:</b> {esc(answer['asked'])}<br>"
                       f"<b>{esc(answer['status'])}:</b> {esc(answer['answer'])}</div>")
        for question in doc.get("questions", []):
            out.append(f"<div class=card><b>Question:</b> {esc(question['question'])}<br>"
                       f"<span class=meta>Why it matters: {esc(question['why_it_matters'])}. "
                       f"If unanswered: {esc(question['default_if_unanswered'])}</span></div>")
    if review:
        rel, doc = review
        out += [f"<h3>The checker</h3><p>{esc(doc.get('summary', ''))} "
                f"<span class=meta>({esc(rel)})</span></p>"]
        for item in doc.get("reviews", []):
            kind = "good" if item["verdict"] == "holds" else "bad"
            out.append(f"<div class=card><span class={kind}><b>{esc(item['verdict'])}</b></span>: "
                       f"{esc(item.get('plain_claim', ''))}<br><span class=meta>"
                       f"{esc(item.get('what_would_resolve', ''))}</span></div>")
    docs = line_documents(program, lid, line)
    out.append("<h3>Documents</h3>")
    if docs:
        out.append("<ul>")
        for rel, kind in docs:
            if is_repo_file(program, rel):
                out.append(f"<li><a href='../docs/{esc(rel)}.html'>{esc(rel)}</a> "
                           f"<span class=meta>{esc(kind)}</span></li>")
            else:
                out.append(f"<li><code>{esc(rel)}</code> <span class=meta>{esc(kind)}</span></li>")
        out.append("</ul>")
    else:
        out.append("<p class=none>None yet.</p>")
    out.append("<h3>Recent runs</h3><table><tr><th>Run</th><th>Headline</th></tr>")
    out += [f"<tr><td>{esc(Path(rel).stem)}</td><td>{esc(doc.get('headline', ''))}</td></tr>"
            for rel, doc in reversed(summaries[-10:])] or [
        "<tr><td colspan=2 class=none>none yet</td></tr>"]
    out.append("</table>")
    failed = [r for r in runs[:10] if json.loads(r["problems"] or "[]")]
    if failed:
        out.append("<h3>Service runs with problems</h3><ul>")
        out += [f"<li>{esc(r['stem'])}: {esc('; '.join(json.loads(r['problems'])))}</li>"
                for r in failed]
        out.append("</ul>")
    instruct = form(program, "instruct", f"Instruct {lid}", lid,
                    "<textarea name=text required placeholder='An instruction for this line; it is "
                    "added to the line&#39;s instruction file and starts the line'></textarea>")
    if instruct:
        out += ["<h3>Instruct this line</h3>", instruct]
    if line.get("notes"):
        out.append("<h3>Notes</h3>")
        out += [f"<div class=card><span class=meta>{esc(n.get('night', n['at'][:10]))}, "
                f"{esc(n['by'])}</span><br>{esc(n.get('text', ''))}</div>"
                for n in reversed(line["notes"][-8:])]
    return page(f"{lid} · {line.get('name', '')}", "\n".join(out))


def doc_page(program: Program, rel: str, lines_using: list[str]) -> str:
    back = " · ".join(f"<a href='../{'../' * rel.count('/')}lines/{esc(lid)}.html'>{esc(lid)}</a>"
                      for lid in lines_using)
    body = (f"<p class=meta>{back}</p><h1>{esc(rel)}</h1>"
            f"<div class=doc>{render_file(program.repo / rel)}</div>")
    return page(rel, body)


def write_program_pages(folder: Path, conn: sqlite3.Connection, program: Program,
                        notes: list[str]) -> None:
    """A page per line, and a page per document lines keep."""
    records, _ = load_ledger(program)
    lines = fold_lines(records.get("lines.jsonl", []))
    sessions, reviews = line_documents_by_kind(program)
    base = folder / program.name
    used: dict[str, list[str]] = {}
    for lid, line in lines.items():
        target = base / "lines" / f"{lid}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(line_page(conn, program, lid, line, sessions.get(lid, []),
                                    reviews.get(lid, []), notes))
        for rel, _ in line_documents(program, lid, line):
            if is_repo_file(program, rel):
                used.setdefault(rel, []).append(lid)
    for rel, lids in used.items():
        target = base / "docs" / f"{rel}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(doc_page(program, rel, sorted(set(lids))))


def write(state: Path, conn: sqlite3.Connection, programs: list[Program],
          looks: dict[str, list[str]], errors: dict[str, list[str]]) -> Path:
    """Write the dashboard and each program's map; return the page's path."""
    folder = state / "dashboard"
    folder.mkdir(parents=True, exist_ok=True)
    sections = []
    for program in programs:
        mapview.write(program, folder / program.name / "map.html", 14)
        write_program_pages(folder, conn, program, looks.get(program.name, []))
        sections.append(program_section(conn, program, looks.get(program.name, [])))
    for name, lines in errors.items():
        sections.append(f"<h2>{esc(name)}</h2><div class=needs><div class='card bad'>"
                        f"<b>The last look failed</b> (tried again at the next):"
                        f"<pre>{esc(chr(10).join(lines))}</pre></div></div>")
    text = page("RoboRobo service",
                "<h1>RoboRobo service</h1>"
                f"<p class=meta>Last look {datetime.now(ZoneInfo(programs[0].timezone) if programs else None):%b %-d, %H:%M %Z}. "
                "This page refreshes every minute. A line's name opens its page, and "
                "<span class=new>new</span> marks lines that changed since you last opened "
                "theirs; Discuss "
                "opens a Claude Code thread in your clone.</p>" + "\n".join(sections),
                refresh=True)
    path = folder / "index.html"
    path.write_text(text)
    return path


class QuietHandler(SimpleHTTPRequestHandler):
    """No line in the service's log for every page view."""

    def log_message(self, format, *args):  # noqa: A002 (the base class's name)
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # always the latest look
        super().end_headers()

    def do_POST(self):  # noqa: N802 (the base class's name)
        """One action from a form on the page (roborobo.actions)."""
        if self.path != "/action":
            return self.send_error(404)
        length = int(self.headers.get("Content-Length", 0))
        if length > 100_000:
            return self.send_error(413)
        sent = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        fields = {key: values[0] for key, values in sent.items()}
        try:
            said, code = actions.perform(self.state, self.repos, fields), 200
        except actions.Refused as refused:
            said, code = f"Not done: {refused}", 403
        forget = ("<p><a href='#' onclick=\"try{localStorage.removeItem('roborobo:token')}"
                  "catch(e){};this.textContent='Forgotten.';return false\">Forget the token in "
                  "this browser</a></p>") if code == 403 else ""
        body = page("RoboRobo service", f"<h1>{esc(said)}</h1><p class=meta>The dashboard shows "
                    "it after the service's next look.</p><p><a href='/index.html'>← back</a></p>"
                    + forget).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_http(state: Path, host: str, port: int, repos: list[Path] = ()) -> ThreadingHTTPServer:
    """Serve the dashboard folder in a background thread, and carry out the
    scientist's actions from it."""
    folder = state / "dashboard"
    folder.mkdir(parents=True, exist_ok=True)
    QuietHandler.state, QuietHandler.repos = state, list(repos)
    handler = partial(QuietHandler, directory=str(folder))
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
