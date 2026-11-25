"""The service's dashboard: every line's state, then each line's own page.

``program serve`` writes ``<state>/dashboard/index.html`` after every look,
and each program's map beside it (``<state>/dashboard/<program>/map.html``),
and serves the folder over HTTP. The pages are built from the records and
the service's database; no prose is written here.

The home page is a list and nothing is answered on it: per program, anything
wrong with the program as a whole (paused, failed jobs), then its lines
grouped by state: stopped, waiting on the scientist, running, queued, idle,
with the paused, closed and dropped ones folded away. Each row says what
the line is for and what happens next.

Each line has a page (``<program>/lines/<line>.html``) meant to be read top
to bottom: its state in a sentence; what it is for (the agent's ``purpose``,
then the scientist's own words); where it stands; its latest run, with the
checker's view of it or why it wasn't checked; the scientist's turn (each
question of that run with its context, collapsing to the answer once
answered, and a box for anything else); earlier runs. A bar fixed to the
foot of the window says what is saved and holds Start, the only thing that
runs the line. Documents, notes, pause and close are folded under More.

Every document a line keeps (files in ``lines/<line>/``, files its records
point to, its runs' summaries and reviews) gets a page of its own
(``<program>/docs/<path>.html``): Markdown rendered, JSON laid out, anything
else as text.
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
from urllib.parse import parse_qs, quote, urlencode, urlsplit
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
form.act.wide { display: block; margin-right: 0; }
form.act textarea { display: block; box-sizing: border-box; width: 100%; min-height: 80px;
  margin-bottom: 4px; font: inherit; }
.said { background: var(--good); color: var(--surface); border-radius: 8px; padding: 10px 14px;
  margin-bottom: 12px; }
.status { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
tr.inactive td { opacity: .75; }
.state { display: inline-block; border-radius: 10px; padding: 1px 9px; font-size: .8rem;
  font-weight: 600; color: #fff; background: var(--muted); white-space: nowrap; }
.state-running { background: var(--good); } .state-you { background: var(--warn); }
.state-stuck { background: var(--bad); } .state-queued, .state-jobs { background: var(--accent); }
.state-idle, .state-closed, .state-paused, .state-dropped, .state-proposed { background: var(--muted); }
a.term { color: inherit; text-decoration: underline dotted; cursor: help; }
.ahead { margin-top: 6px; font-size: .88rem; }
form.act button { font: inherit; font-weight: 600; padding: 5px 14px; border-radius: 6px;
  border: 1px solid var(--accent); background: var(--accent); color: #fff; cursor: pointer;
  box-shadow: 0 1px 2px rgba(0, 0, 0, .25); }
form.act button:hover { filter: brightness(1.12); }
form.act button:active { box-shadow: none; transform: translateY(1px); }
.token-row { display: block; margin: 6px 0; font-size: .9rem; color: var(--warn); }
.token-row input { font: inherit; width: 22em; max-width: 100%; }
form.act button:disabled { opacity: .6; cursor: default; }
.new { background: var(--accent); color: var(--surface); border-radius: 4px; padding: 0 5px;
  font-size: .75rem; margin-left: 4px; }
details summary { cursor: pointer; } details[open] summary { color: var(--muted); }
div.doc { background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
  padding: 4px 18px; overflow-x: auto; }
div.doc table { width: auto; } pre { white-space: pre-wrap; word-break: break-word; }
.said { position: sticky; top: 8px; z-index: 2; box-shadow: 0 2px 6px rgba(0, 0, 0, .25); }
.row { background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 14px; margin: 6px 0; }
.row-head a { text-decoration: none; font-size: 1.02rem; } .row-head .state { margin-left: 6px; }
.row-now { font-weight: 600; margin-top: 2px; } .row-what { margin-top: 4px; }
.row-facts { margin-top: 4px; }
details.inactive { margin-top: 20px; } details.inactive .row { opacity: .8; }
.now { font-size: 1.05rem; font-weight: 600; }
blockquote { margin: 8px 0; padding: 4px 14px; border-left: 3px solid var(--line); }
.who { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .04em;
  margin: 14px 0 4px; }
.reply { border-left: 3px solid var(--line); padding: 2px 12px; margin: 8px 0; }
.turn { border-left: 4px solid var(--warn); } .turn.done { border-left-color: var(--good); }
.answered { margin: 8px 0 2px; white-space: pre-wrap; }
details.card > summary { color: var(--fg); }
details.more { margin-top: 28px; } details.more > summary { font-weight: 600; }
body { padding-bottom: 90px; }
.bar { position: fixed; left: 0; right: 0; bottom: 0; background: var(--surface);
  border-top: 2px solid var(--accent); box-shadow: 0 -2px 8px rgba(0, 0, 0, .15); }
.bar-in { max-width: 1100px; margin: 0 auto; padding: 10px 16px; display: flex; gap: 14px;
  align-items: center; justify-content: space-between; font-weight: 600; }
.bar form.act { margin: 0; flex: none; }
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
  document.querySelectorAll(".row[data-line]").forEach(function (row) {
    if (!row.closest("details.inactive") &&
        localStorage.getItem("roborobo:" + row.dataset.line) !== row.dataset.sig) {
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
    kind = "act wide" if fields else "act"
    return (f"<form class='{kind}' method=post action='/action'>{inputs}{fields}"
            f"<button>{esc(button)}</button></form>")


# Fills the token into a form when it is sent. The token is asked for once and
# kept in this browser; "forget" removes it.
ACT = """<script>
// The token is asked for in the page itself (a browser may silently block a
// pop-up question, and then the button would do nothing).
document.querySelectorAll("form.act").forEach(function (f) {
  f.addEventListener("submit", function (e) {
    var t = null, button = f.querySelector("button");
    try { t = localStorage.getItem("roborobo:token"); } catch (x) {}
    var ask = f.querySelector("input.token-ask");
    if (ask && ask.value.trim()) {
      t = ask.value.trim();
      try { localStorage.setItem("roborobo:token", t); } catch (x) {}
    }
    if (!t) {
      e.preventDefault();
      if (!ask) {
        var row = document.createElement("label");
        row.className = "token-row";
        row.textContent = "Your dashboard token, once for this browser: ";
        ask = document.createElement("input");
        ask.type = "password"; ask.className = "token-ask"; ask.autocomplete = "off";
        row.appendChild(ask);
        f.insertBefore(row, button);
      }
      ask.focus();
      return;
    }
    f.querySelector("[name=token]").value = t;
    button.textContent = "Sending…";
    setTimeout(function () { button.disabled = true; }, 0);
  });
});
</script>"""


class Marks:
    """Turns record text into HTML that links what it mentions: a term (or an
    alias) to its definition, shown on hover; a line's id or name to the
    line's page; a document's path or file name to its page."""

    def __init__(self, program: Program, lines: dict[str, dict], terms: list[dict],
                 base: str):
        self.base = base
        # lowercased text -> kind, href, title, and the exact spelling if only that counts
        self.targets: dict[str, tuple[str, str, str, str | None]] = {}

        def add(text: str, kind: str, href: str, title: str = "", exact: bool = False) -> None:
            self.targets.setdefault(text.lower(), (kind, href, title, text if exact else None))

        for lid, line in lines.items():
            add(lid, "line", f"lines/{lid}.html", line.get("name", ""))
            for rel, _ in line_documents(program, lid, line):
                if is_repo_file(program, rel):
                    add(rel, "doc", f"docs/{rel}.html")
                    add(Path(rel).name, "doc", f"docs/{rel}.html", rel)
        for term in terms:
            for name in [term["term"], *term.get("aliases", [])]:
                add(name, "term", f"terms.html#{term['term_id']}", term["definition"])
        for lid, line in lines.items():  # a line's name, unless a term has it
            name = line.get("name", "")
            if len(name) >= 6:  # as written only: "Heater" the line, not every heater
                add(name, "line", f"lines/{lid}.html", f"line {lid}", exact=True)
        names = sorted(self.targets, key=len, reverse=True)
        self.pattern = re.compile(
            r"(?<![\w/.-])(" + "|".join(re.escape(n) for n in names) + r")(?![\w/-]|\.\w)",
            re.IGNORECASE) if names else None

    def __call__(self, text) -> str:
        text = str(text or "")
        if not self.pattern:
            return esc(text)
        out, at = [], 0
        for found in self.pattern.finditer(text):
            kind, href, title, exact = self.targets[found[1].lower()]
            if exact and found[1] != exact:
                continue
            out.append(esc(text[at:found.start()]))
            out.append(f"<a class={kind} href='{esc(self.base + href)}'"
                       + (f" title='{esc(title)}'" if title else "") + f">{esc(found[1])}</a>")
            at = found.end()
        out.append(esc(text[at:]))
        return "".join(out)


def program_terms(records: dict) -> list[dict]:
    """The program's terms, the latest definition of each, by name."""
    latest: dict[str, dict] = {}
    for record in records.get("terms.jsonl", []):
        latest[record["term_id"]] = record
    return sorted(latest.values(), key=lambda t: t["term"].lower())


def terms_page(program: Program, terms: list[dict], m: Marks) -> str:
    out = ["<p class=meta><a href='../index.html'>← all lines</a></p>",
           f"<h1>Terms used in {esc(program.name)}</h1>",
           "<p class=meta>The names the records use, as the program defines them "
           "(ledger/terms.jsonl). On every page, a dotted underline marks a term; hover for its "
           "definition.</p>"]
    for term in terms:
        also = (f" <span class=meta>also: {esc(', '.join(term['aliases']))}</span>"
                if term.get("aliases") else "")
        out.append(f"<div class=card id='{esc(term['term_id'])}'><b>{esc(term['term'])}</b>{also}"
                   f"<br>{esc(term['definition'])}</div>")
    if not terms:
        out.append("<p class=none>None defined yet.</p>")
    return page(f"Terms · {program.name}", "\n".join(out))


def related(lines: dict[str, dict], lid: str, base: str) -> str:
    """Links to the lines this one came from, branches into and feeds."""
    def link(other: str) -> str:
        name = lines.get(other, {}).get("name", "")
        return f"<a class=line href='{esc(base)}lines/{esc(other)}.html'>{esc(other)} {esc(name)}</a>"
    line = lines[lid]
    parts = []
    if line.get("parent"):
        parts.append("from " + link(line["parent"]))
    branches = [o for o, l in lines.items() if l.get("parent") == lid]
    if branches:
        parts.append("branches: " + ", ".join(link(o) for o in branches))
    if line.get("feeds"):
        parts.append("feeds " + ", ".join(link(o) for o in line["feeds"]))
    fed_by = [o for o, l in lines.items() if lid in l.get("feeds", [])]
    if fed_by:
        parts.append("fed by " + ", ".join(link(o) for o in fed_by))
    return "; ".join(parts)


def ahead(line: dict, m: Marks) -> str:
    """What the line does next and what would close it, or that nobody said."""
    missing = "<span class=none>not recorded</span>"
    return (f"<b>Next:</b> {m(line['next']) if line.get('next') else missing}<br>"
            f"<b>Closes when:</b> {m(line['closes_when']) if line.get('closes_when') else missing}")


# After an action the page comes back with ?said=...; show it once, at the top.
SAID = """<script>
(function () {
  var said = new URLSearchParams(location.search).get("said");
  if (!said) return;
  var banner = document.createElement("div");
  banner.className = "said";
  banner.textContent = said;
  document.body.insertBefore(banner, document.body.firstChild);
  history.replaceState(null, "", location.pathname + location.hash);
})();
</script>"""


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


ENTRY_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(:\d{2})?")


def instruction_entries(program: Program, lid: str) -> list[dict]:
    """What the scientist has written in the line's own instruction file,
    oldest first: each entry's heading, when it was written, the question it
    answers (if it quotes one) and its text."""
    path = program.repo / "lines" / lid / "instruction.md"
    if not path.is_file():
        return []
    entries = []
    for entry in path.read_text(errors="replace").split("\n## ")[1:]:
        heading, _, body = entry.partition("\n")
        found = ENTRY_TIME.search(heading)
        at = datetime.strptime(f"{found[1]} {found[2]}{found[3] or ':00'}", "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=ZoneInfo(program.timezone)).timestamp() if found else None
        rows = body.strip().splitlines()
        question = " ".join(row[1:].strip() for row in rows if row.startswith(">"))
        text = body.strip()
        if question and "Answer:" in rows:
            text = "\n".join(rows[rows.index("Answer:") + 1:]).strip()
        if text:
            entries.append({"heading": heading.strip(), "at": at, "question": question,
                            "text": text, "to_the_minute": bool(found and not found[3])})
    return entries


def your_turn(conn: sqlite3.Connection, program: Program, lid: str,
              summary: tuple[str, dict] | None) -> dict:
    """What the line is waiting to hear and what it has been told since its
    last run: ``questions`` pairs each question of the latest run with the
    scientist's answer to it (or None), ``others`` is everything else said
    since."""
    last = conn.execute("SELECT started FROM runs WHERE program = ? AND line = ? "
                        "ORDER BY id DESC", (program.name, lid)).fetchone()
    summarised = committed_at(program.repo, summary[0]) if summary else 0
    since = datetime.fromisoformat(last["started"]).timestamp() if last else summarised
    path = program.repo / "lines" / lid / "instruction.md"
    written = committed_at(program.repo, path.relative_to(program.repo).as_posix()) \
        if path.is_file() else 0
    waiting = conn.execute("SELECT 1 FROM events WHERE program = ? AND line = ? AND kind = "
                           "'instruction' AND status IN ('held', 'pending')",
                           (program.name, lid)).fetchone()

    def is_new(entry: dict) -> bool:
        if entry["at"] is None:  # no time in its heading: go by the file
            return written > since
        # A heading written by hand carries minutes only: an entry in the run's
        # own minute is new only if the service still holds an instruction.
        if entry["to_the_minute"] and entry["at"] < since:
            return entry["at"] + 60 > since and bool(waiting)
        return entry["at"] >= int(since)

    new = [entry for entry in instruction_entries(program, lid) if is_new(entry)]
    asked = summary[1].get("questions", []) if summary and summarised >= answered_after(program) \
        else []
    same = lambda text: " ".join(str(text).split())  # noqa: E731
    questions = []
    for question in asked:
        answers = [e for e in new if same(e["question"]) == same(question["question"])]
        questions.append((question, answers[-1] if answers else None))
    asked_texts = {same(question["question"]) for question in asked}
    others = [e for e in new if same(e["question"]) not in asked_texts or not e["question"]]
    return {"questions": questions, "others": others,
            "answered": sum(1 for _, answer in questions if answer)}


def line_state(conn: sqlite3.Connection, program: Program, lid: str, line: dict,
               summary: tuple[str, dict] | None, open_jobs: list[dict],
               notes: list[str]) -> tuple[str, str, str]:
    """Where a line is, as one of a few states: (key, label, detail). The key
    picks the badge's colour; the detail says what happens next."""
    status = line.get("status", "active")
    if status == "proposed":
        return "you", "Proposed by the agent", "yours to make active or drop; it doesn't run until then"
    if status != "active":
        return status, status.capitalize(), ""
    name = program.name
    held = conn.execute("SELECT reason FROM paused_lines WHERE program = ? AND line = ?",
                        (name, lid)).fetchone()
    if held:
        return "stuck", "Stopped: needs fixing", held["reason"]
    running = conn.execute("SELECT stem, started FROM runs WHERE program = ? AND line = ? AND "
                           "ended IS NULL ORDER BY id DESC", (name, lid)).fetchone()
    if running:
        return "running", "Running", f"run {running['stem']} since {local(program, running['started'])}"
    events = conn.execute("SELECT kind, status FROM events WHERE program = ? AND line = ? AND "
                          "status IN ('pending', 'held')", (name, lid)).fetchall()
    if any(e["kind"] == "start" for e in events):
        return "queued", "Starting", "you pressed Start; the run begins within a minute"
    turn = your_turn(conn, program, lid, summary)
    asked, answered, told = len(turn["questions"]), turn["answered"], len(turn["others"])
    saved = answered + told or any(e["status"] == "held" for e in events)
    if asked or saved:
        parts = []
        if asked:
            parts.append(f"{answered} of {plural(asked, 'question')} answered")
        if told:
            parts.append(f"{plural(told, 'instruction')} saved")
        if saved:
            what = "Nothing runs until you press Start."
        elif events or open_jobs:
            what = "It still runs when its jobs end, taking its stated defaults."
        else:
            what = "Nothing runs until you press Start."
        label = "Waiting on you" if asked > answered else "Ready when you are"
        return "you", label, ", ".join(parts or ["your instructions are saved"]) + ". " + what
    if events:
        why = next((n.removeprefix("waiting: ") for n in notes
                    if n.startswith(f"waiting: {lid} ")), "starts at the service's next look")
        return "queued", "Queued", why
    if open_jobs:
        hosts = ", ".join(sorted({j["host"] for j in open_jobs}))
        return "jobs", f"Waiting on {plural(len(open_jobs), 'job')}", \
            f"on {hosts}; the line runs again when one ends"
    return "idle", "Idle", "nothing queued; it runs when you press Start on its page"


def badge(state: tuple[str, str, str]) -> str:
    key, label, detail = state
    return (f"<span class='state state-{esc(key)}'>{esc(label)}</span>"
            + (f"<br><span class=meta>{esc(detail)}</span>" if detail else ""))


def page(title: str, body: str, refresh: bool = False) -> str:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            + ("<meta http-equiv=refresh content=60>" if refresh else "")
            + f"<title>{esc(title)}</title><style>{STYLE}</style></head><body>{body}"
            + (ACT if "<form class='act" in body else "") + SAID + "</body></html>")


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
              "questions. If we settle on an instruction, show me its exact text first. When I "
              f"say yes, append it to lines/{lid}/instruction.md as a new entry at the end, "
              "under a heading `## <date> <time>, " + program.scientist + ", from a discussion` "
              "(create the file with a `# Instructions for " + lid + "` title if it doesn't "
              "exist), commit only that file, and give me the `git push` command to run: you "
              "can't push from here. Then tell me to press Start on " + lid + " on the dashboard "
              "when I'm ready; nothing runs until I do.")
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


def verdict_counts(review: dict) -> str:
    verdicts = [r["verdict"] for r in review.get("reviews", [])]
    return " ".join(f"<span class={'good' if v == 'holds' else 'bad'}>{verdicts.count(v)} {v}</span>"
                    for v in ("holds", "doubtful", "broken") if verdicts.count(v)) \
        or "<span class=none>no claims to check</span>"


# Groups of the home page, in order: (state keys, heading).
GROUPS = [(("stuck",), "Stopped: needs fixing"), (("you",), "Waiting on you"),
          (("running",), "Running"), (("queued", "jobs"), "Queued, or waiting on jobs"),
          (("idle",), "Idle")]


def program_section(conn: sqlite3.Connection, program: Program, notes: list[str]) -> str:
    """The home page's part for one program: its lines, grouped by what state
    each is in. Nothing is answered here; each line's page has its story and
    its questions."""
    records, _ = load_ledger(program)
    lines = fold_lines(records.get("lines.jsonl", []))
    all_sessions, _ = line_documents_by_kind(program)
    sessions, reviews = latest_documents(program)
    m = Marks(program, lines, program_terms(records), f"{program.name}/")
    open_jobs = [j for j in cluster.load_jobs(program) if not j.get("ended_at")]
    runs = conn.execute("SELECT * FROM runs WHERE program = ? ORDER BY id DESC",
                        (program.name,)).fetchall()
    out = [f"<h2>{esc(program.name)}</h2>",
           f"<p class=meta>{esc(program.title)} · <a href='{esc(program.name)}/map.html'>"
           f"program map</a> · <a href='{esc(program.name)}/terms.html'>terms</a></p>"]
    paused = conn.execute("SELECT at, reason FROM paused WHERE program = ?",
                          (program.name,)).fetchone()
    trouble = []
    if paused:
        trouble.append(f"<b>The whole program is paused</b> since {esc(local(program, paused['at']))}: "
                       f"{m(paused['reason'])}<br><span class=meta>Fix it in the service's "
                       f"clone, then <code>program serve --resume {esc(program.name)}</code>"
                       "</span>" + form(program, "resume", "Resume"))
    trouble += [m(note) for note in notes if note.startswith(("job problem:", "error"))]
    if trouble:
        out += ["<div class=needs>"] + [f"<div class='card bad'>{text}</div>" for text in trouble] \
            + ["</div>"]

    rows: dict[str, list[str]] = {}
    for lid, line in sorted(lines.items()):
        session, review = sessions.get(lid), reviews.get(lid)
        jobs = [j for j in open_jobs if j.get("line_id") == lid]
        key, label, detail = line_state(conn, program, lid, line, session, jobs, notes)
        what = line.get("purpose") or line.get("state") or line.get("question") or ""
        facts = []
        if line.get("next") and key not in ("closed", "dropped"):
            facts.append(f"<b>Next:</b> {m(line['next'])}")
        if session:
            reviewed = review and Path(review[0]).stem == Path(session[0]).stem
            facts.append("Last run " + esc(Path(session[0]).stem[:10]) + ": "
                         + (f"checker {verdict_counts(review[1])}" if reviewed
                            else "<span class=warn>not checked</span>"))
        heard = last_heard(program, lid, line, all_sessions.get(lid, []), runs)
        quiet = heard and (datetime.now(timezone.utc).timestamp() - heard) > QUIET_DAYS * 86400
        if heard:
            facts.append(f"<span class={'warn' if quiet and key == 'idle' else 'meta'}>last heard "
                         f"{esc(when(heard, program.timezone))}</span>")
        sig = signature(line, session[0] if session else None, review[0] if review else None)
        href = f"{esc(program.name)}/lines/{esc(lid)}.html"
        rows.setdefault(key, []).append(
            f"<div class='row' data-line='{esc(program.name)}/{esc(lid)}' data-sig='{sig}'>"
            f"<div class=row-head><a href='{href}'><b>{esc(lid)}</b> · {esc(line.get('name', ''))}"
            f"</a><span class=new hidden>new</span> <span class='state state-{esc(key)}'>"
            f"{esc(label)}</span></div>"
            + (f"<div class=row-now>{m(detail[:1].upper() + detail[1:])}</div>" if detail else "")
            + f"<div class=row-what>{m(mapview.short(what, 260))}</div>"
            + (f"<div class='meta row-facts'>{' · '.join(facts)}</div>" if facts else "")
            + "</div><!--/row-->")
    for keys, heading in GROUPS:
        group = [row for key in keys for row in rows.get(key, [])]
        if group:
            out += [f"<h3>{esc(heading)} ({len(group)})</h3>", *group]
    rest = [row for key in ("paused", "closed", "dropped") for row in rows.get(key, [])]
    if rest:
        out += [f"<details class=inactive><summary>Show the {len(rest)} paused, closed and "
                "dropped lines</summary>", *rest, "</details>"]
    if not lines:
        out.append("<p class=none>No lines yet.</p>")
    out.append(MARK_NEW)
    return "\n".join(out)


def run_story(program: Program, m: Marks, rel: str, doc: dict, review: tuple[str, dict] | None,
              run, base: str) -> str:
    """One run, told in order: what the agent reported, then what the checker
    made of it (or that it wasn't checked, and why)."""
    out = [f"<p><b>{m(doc.get('headline', ''))}</b></p>"]
    if doc.get("answers"):
        out.append("<details><summary>The agent's replies to the "
                   f"{plural(len(doc['answers']), 'thing')} you had asked</summary>")
        for answer in doc["answers"]:
            status = answer["status"].replace("_", " ")
            out.append(f"<div class=reply><span class=meta>You asked:</span> {m(answer['asked'])}"
                       f"<br><span class=meta>The agent ({esc(status)}):</span> "
                       f"{m(answer['answer'])}</div>")
        out.append("</details>")
    out.append(f"<p class=meta><a href='{esc(base)}docs/{esc(rel)}.html'>The run's full "
               "summary</a></p>")
    out.append("<p class=who>The checker (a second agent that tries to break the run's claims)</p>")
    if review:
        review_rel, checked = review
        out.append(f"<p>{m(checked.get('summary', ''))} <a class=meta href='{esc(base)}docs/"
                   f"{esc(review_rel)}.html'>full review</a></p>")
        for item in checked.get("reviews", []):
            kind = "good" if item["verdict"] == "holds" else "bad"
            out.append(f"<div class=reply><span class={kind}><b>{esc(item['verdict'])}</b></span>: "
                       f"{m(item.get('plain_claim', ''))}"
                       + (f"<br><span class=meta>{m(item['what_would_resolve'])}</span>"
                          if item.get("what_would_resolve") else "") + "</div>")
    else:
        problems = json.loads(run["problems"] or "[]") if run else []
        why = ("The run stopped on a problem (" + "; ".join(problems) + ") and was put right "
               "afterwards, so the checker's step never ran.") if problems else \
            "There is no review of this run."
        out.append(f"<p class=warn>Not checked. {esc(why)}</p>")
    return "\n".join(out)


def line_page(conn: sqlite3.Connection, program: Program, lid: str, line: dict,
              summaries: list[tuple[str, dict]], reviews: list[tuple[str, dict]],
              notes: list[str], lines: dict[str, dict] | None = None,
              m: Marks | None = None) -> str:
    """One line, read top to bottom: its state in a sentence, what it is for,
    where it stands, its latest run with the checker's view, the scientist's
    turn, then earlier runs. A bar at the foot of the window says what will run and holds Start.
    Everything else is folded under More."""
    lines = lines or {lid: line}
    m = m or Marks(program, lines, [], "../")
    runs = conn.execute("SELECT * FROM runs WHERE program = ? AND line = ? ORDER BY id DESC",
                        (program.name, lid)).fetchall()
    by_stem = {r["stem"]: r for r in runs}
    reviewed = {Path(rel).stem: (rel, doc) for rel, doc in reviews}
    open_jobs = [j for j in cluster.load_jobs(program)
                 if not j.get("ended_at") and j.get("line_id") == lid]
    heard = last_heard(program, lid, line, summaries, runs)
    latest = summaries[-1] if summaries else None
    key, label, detail = state_now = line_state(conn, program, lid, line, latest, open_jobs, notes)
    stem = Path(latest[0]).stem if latest else None
    status = line.get("status", "active")
    discuss = (f"<a href='{esc(discuss_link(program, lid, line.get('name', lid), stem, stem in reviewed))}'>"
               "Discuss it in a Claude thread</a>")
    sig = signature(line, latest[0] if latest else None, reviews[-1][0] if reviews else None)
    out = [mark_seen(f"{program.name}/{lid}", sig),
           "<p class=meta><a href='../../index.html'>← all lines</a></p>",
           f"<h1>{esc(lid)} · {esc(line.get('name', ''))}</h1>",
           f"<p class=now><span class='state state-{esc(key)}'>{esc(label)}</span> {m(detail[:1].upper() + detail[1:])}</p>"]
    if key == "stuck":
        out.append("<div class=needs><div class='card bad'>Fix it in the line's worktree, then "
                   f"<code>program serve --resume {esc(program.name)}:{esc(lid)}</code>"
                   + form(program, "resume", f"Resume {lid}", lid) + "</div></div>")

    out.append("<h3>What this line is for</h3>")
    if line.get("purpose"):
        out.append(f"<p>{m(line['purpose'])}</p>")
    else:
        out.append("<p class=none>The agent hasn't written this yet; it will at the line's "
                   "next run.</p>")
        if line.get("reason"):
            out.append(f"<p><span class=meta>Why it was opened:</span> {m(line['reason'])}</p>")
    if line.get("question"):
        source = esc(line.get("question_source") or "the question it was opened with")
        out.append(f"<blockquote>{m(line['question'])}<br><span class=meta>{source}</span>"
                   "</blockquote>")
    out += ["<h3>Where it stands</h3>",
            f"<p>{m(line.get('state', '')) or '<span class=none>not recorded</span>'}</p>",
            f"<p>{ahead(line, m)}</p>"]
    if open_jobs:
        out.append("<p><b>Jobs running:</b> " + "; ".join(
            f"{esc(j['experiment_id'])} job {esc(j['job_id'])} on {esc(j['host'])} "
            f"({esc(j.get('state', ''))} {esc(j.get('elapsed', ''))})" for j in open_jobs) + "</p>")

    out.append("<h3>Its latest run</h3>")
    if not summaries:
        out.append("<p class=none>No runs yet.</p>")
    told = [(rel, doc) for rel, doc in reversed(summaries)]
    earlier = []
    for index, (rel, doc) in enumerate(told[:6]):
        name = Path(rel).stem
        story = run_story(program, m, rel, doc, reviewed.get(name), by_stem.get(name), "../")
        if index == 0:
            out.append(f"<div class=card><p class=meta>{esc(name)}</p>{story}</div>")
        else:
            check = verdict_counts(reviewed[name][1]) if name in reviewed \
                else "<span class=warn>not checked</span>"
            earlier.append(f"<details class=card><summary><span class=meta>{esc(name)}</span> "
                           f"{esc(mapview.short(doc.get('headline', ''), 150))} · {check}"
                           f"</summary>{story}</details>")

    turn = your_turn(conn, program, lid, latest)
    acts = bool(actions.token())
    out.append("<h3 id=your-turn>Your turn</h3>")
    if status == "proposed":
        out.append("<div class='card turn'>The agent proposed this line. It runs only if you "
                   "make it active.<br>"
                   + form(program, "line-status", "Make it active", lid, status="active")
                   + form(program, "line-status", "Drop it", lid, status="dropped") + "</div>")
    elif status != "active":
        out.append(f"<p class=none>This line is {esc(status)}; reopen it under More to "
                   "instruct it.</p>")
    else:
        if not turn["questions"] and not turn["others"]:
            out.append("<p class=meta>The line has no questions for you. Tell it something "
                       "below if you want, or just press Start.</p>")
        for number, (question, answer) in enumerate(turn["questions"], 1):
            box = form(program, "instruct", "Save answer", lid,
                       "<textarea name=text required placeholder='Your answer. Saving starts "
                       "nothing.'></textarea>", question=question["question"], anchor="your-turn")
            head = (f"<b>Question {number} of {len(turn['questions'])}.</b> "
                    f"{m(question['question'])}<br><span class=meta>Why it matters: "
                    f"{m(question['why_it_matters'])}<br>If you don't answer: "
                    f"{m(question['default_if_unanswered'])}</span>")
            if answer:
                change = f"<details><summary>Change your answer</summary>{box}</details>" if box else ""
                out.append(f"<div class='card turn done'>{head}<p class=answered><span class=good>"
                           f"✓ You answered:</span> {esc(answer['text'])}</p>{change}</div>")
            else:
                out.append(f"<div class='card turn'>{head}{box}</div>")
        if turn["others"]:
            out.append("<div class='card turn done'><b>Also saved for its next run</b><ul>"
                       + "".join(f"<li>{esc(e['text'])}" + (
                           f"<br><span class=meta>in reply to: {esc(e['question'])}</span>"
                           if e["question"] else "") + "</li>" for e in turn["others"])
                       + "</ul></div>")
        more = form(program, "instruct", "Save", lid,
                    "<textarea name=text required placeholder='Anything else for this line. "
                    "Saving starts nothing.'></textarea>", anchor="your-turn")
        if more:
            out.append(f"<div class=card><b>Anything else?</b>{more}"
                       f"<p class=meta>Or {discuss}.</p></div>")
        elif not acts:
            out.append(f"<p class=meta>This page is read-only. {discuss}.</p>")

    if earlier:
        out += ["<h3>Earlier runs, newest first</h3>", *earlier]

    out.append("<details class=more><summary>More: documents, earlier runs, notes, pause or "
               "close</summary>")
    links = related(lines, lid, "../")
    out.append(f"<p class=meta>{esc(status)} · last heard "
               f"{esc(when(heard, program.timezone)) if heard else 'never'}"
               + (f" · {links}" if links else "") + f" · {discuss}</p>")
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
    progress = program.repo / "lines" / lid / "progress.md"
    if progress.is_file():
        out += ["<h3>Progress notes</h3>", f"<div class=doc>{render_file(progress)}</div>"]
    if len(told) > 6:
        out.append("<h3>Older runs</h3><table><tr><th>Run</th><th>Headline</th></tr>")
        out += [f"<tr><td><a href='../docs/{esc(rel)}.html'>{esc(Path(rel).stem)}</a></td>"
                f"<td>{m(doc.get('headline', ''))}</td></tr>" for rel, doc in told[6:]]
        out.append("</table>")
    failed = [r for r in runs[:10] if json.loads(r["problems"] or "[]")]
    if failed:
        out.append("<h3>Service runs with problems</h3><ul>")
        out += [f"<li>{esc(r['stem'])}: {esc('; '.join(json.loads(r['problems'])))}</li>"
                for r in failed]
        out.append("</ul>")
    if line.get("notes"):
        out.append("<h3>Notes</h3>")
        out += [f"<div class=card><span class=meta>{esc(n.get('night', n['at'][:10]))}, "
                f"{esc(n['by'])}</span><br>{m(n.get('text', ''))}</div>"
                for n in reversed(line["notes"][-8:])]
    why = "<textarea name=comment required placeholder='Why? Saved as your note on the line'></textarea>"
    changes = {"active": [("closed", f"Close {lid}", why), ("paused", f"Pause {lid}", why)],
               "paused": [("active", f"Resume {lid} (make it active)", ""),
                          ("closed", f"Close {lid}", why)],
               "closed": [("active", f"Reopen {lid}", "")],
               "dropped": [("active", f"Reopen {lid}", "")]}
    forms = [form(program, "line-status", button, lid, fields, status=to)
             for to, button, fields in changes.get(status, [])]
    if any(forms):
        out += [f"<h3>This line is {esc(status)}</h3>", *forms]
    out.append("</details>")

    if status == "active":
        can_start = acts and key not in ("running", "stuck") and label != "Starting"
        if key == "you" or key == "idle":
            asked, answered = len(turn["questions"]), turn["answered"]
            saying = []
            if asked:
                saying.append(f"{answered} of {plural(asked, 'question')} answered")
            if turn["others"]:
                saying.append(f"{plural(len(turn['others']), 'instruction')} saved")
            text = (", ".join(saying) or "Nothing new from you") + ". " + (
                "Unanswered questions take the default shown. " if asked > answered else "") \
                + "Nothing runs until you press Start."
        else:
            text = f"{label}: {detail}" if detail else label
        out.append(f"<div class=bar><div class=bar-in><span>{esc(text)}</span>"
                   + (form(program, "start", f"Start {lid}", lid) if can_start else "")
                   + "</div></div>")
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
    terms = program_terms(records)
    m = Marks(program, lines, terms, "../")
    base.mkdir(parents=True, exist_ok=True)
    (base / "terms.html").write_text(terms_page(program, terms, m))
    for lid, line in lines.items():
        target = base / "lines" / f"{lid}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(line_page(conn, program, lid, line, sessions.get(lid, []),
                                    reviews.get(lid, []), notes, lines, m))
        kept = [rel for rel, _ in line_documents(program, lid, line)]
        kept += [rel for rel, _ in sessions.get(lid, []) + reviews.get(lid, [])]
        for rel in kept:
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
                "This page refreshes every minute. Each line's page has its story, its questions "
                "for you and its Start button; <span class=new>new</span> marks lines that "
                "changed since you last opened theirs.</p>" + "\n".join(sections),
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
        if code == 200:
            # Show it at once: redraw the pages, then go back to where the form was.
            rerender(self.state, self.repos)
            back = urlsplit(self.headers.get("Referer", "")).path or "/index.html"
            self.send_response(303)
            anchor = fields.get("anchor", "")
            mark = f"#{anchor}" if re.fullmatch(r"[\w-]+", anchor) else ""
            self.send_header("Location", f"{back}?{urlencode({'said': said})}{mark}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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


def rerender(state: Path, repos: list[Path]) -> None:
    """Redraw the dashboard now (after an action), with the notes of the
    service's last look."""
    from roborobo import config, service
    saved = state / "looks.json"
    last = json.loads(saved.read_text()) if saved.exists() else {"looks": {}, "errors": {}}
    conn = service.connect(state / "service.db")
    try:
        write(state, conn, [config.load(repo) for repo in repos], last["looks"], last["errors"])
    finally:
        conn.close()


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
