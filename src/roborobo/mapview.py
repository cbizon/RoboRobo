"""The program map: lines of investigation against nights, as one HTML page.

The page is assembled from the records alone (``ledger/lines.jsonl``,
``ledger/terms.jsonl``, ``ledger/experiments.jsonl``, ``sessions/`` and
``reviews/``).  No prose is written here, so the page says what the records
say."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from roborobo.config import Program
from roborobo.records import documents, latest, load_ledger

PAGE = Path(__file__).with_name("map.html")
VERDICT_RANK = {"holds": 1, "doubtful": 2, "broken": 3}
RUN = {"adequate": "ok", "strained": "warn", "inadequate": "bad"}


def label(day: str) -> str:
    parsed = date.fromisoformat(day)
    return f"{parsed:%b} {parsed.day}"


def fold_lines(records: list[dict]) -> dict[str, dict]:
    """Each line's current values: the latest record that sets a field wins;
    artifacts and placed experiments accumulate."""
    lines: dict[str, dict] = {}
    for record in records:
        line = lines.setdefault(record["line_id"], {
            "id": record["line_id"], "status": "active", "feeds": [], "arts": [],
            "placed": {}, "notes": [], "pinned": {}})
        if record["record"] == "open":
            line["by"] = record["by"]
            line["opened"] = record.get("night", record["at"][:10])
        for key in ("name", "question", "question_source", "reason", "parent",
                    "status", "state", "purpose", "next", "closes_when", "feeds"):
            if key in record and record["record"] != "note":
                line[key] = record[key]
        for artifact in record.get("artifacts", []):
            pair = [artifact["kind"], artifact["path"]]
            if pair not in line["arts"]:
                line["arts"].append(pair)
        for pin in record.get("pinned", []):
            if pin.get("removed"):
                line["pinned"].pop(pin["slug"], None)
            else:
                line["pinned"][pin["slug"]] = pin
        for placed in record.get("experiments", []):
            line["placed"][placed["experiment_id"]] = placed.get("title")
        if record["record"] == "note":
            line["notes"].append(record)
    return lines


def tree_order(lines: dict[str, dict]) -> list[dict]:
    """Roots in the order they were opened, each followed by its branches."""
    ordered = []

    def visit(line: dict, depth: int) -> None:
        line["depth"] = depth
        ordered.append(line)
        for child in lines.values():
            if child.get("parent") == line["id"]:
                visit(child, depth + 1)

    for line in lines.values():
        if line.get("parent") not in lines:
            visit(line, 0)
    return ordered


def short(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def build(program: Program) -> dict:
    """Everything the page shows, as plain data."""
    records, problems = load_ledger(program)
    summaries, summary_problems = documents(program, "sessions", "session-summary.schema.json")
    reviews, review_problems = documents(program, "reviews", "review.schema.json")
    if problems or summary_problems or review_problems:
        raise ValueError("the records don't validate; run `program check` first:\n  "
                         + "\n  ".join(problems + summary_problems + review_problems))
    who = {"human": program.scientist, "agent": "the agent", "referee": "the referee"}
    # A night has one summary and review per line run (or one for the whole
    # night, before lines ran separately).
    summaries_by_night: dict[str, list[dict]] = {}
    for _, summary in summaries:
        summaries_by_night.setdefault(summary["date"], []).append(summary)
    reviews_by_night: dict[str, list[dict]] = {}
    for _, review in reviews:
        reviews_by_night.setdefault(review["date"], []).append(review)
    lines = fold_lines(records["lines.jsonl"])

    registrations = {r["experiment_id"]: r for r in records["experiments.jsonl"]
                     if r["record"] == "registration"}
    outcomes = {r["experiment_id"]: r for r in records["experiments.jsonl"]
                if r["record"] == "outcome"}
    line_of = {eid: r["line_id"] for eid, r in registrations.items() if "line_id" in r}
    titles = {eid: r["title"] for eid, r in registrations.items() if "title" in r}
    for line in lines.values():
        for eid, title in line["placed"].items():
            line_of[eid] = line["id"]
            if title:
                titles[eid] = title

    verdicts: dict[str, str] = {}
    reviewed_on: dict[str, dict[str, str]] = {}
    for night in sorted(reviews_by_night):
        for review in reviews_by_night[night]:
            for item in review["reviews"]:
                verdicts[item["claim_id"]] = item["verdict"]
                reviewed_on.setdefault(night, {})[item["claim_id"]] = item["verdict"]

    experiments = {eid: {"title": titles.get(eid) or short(reg["hypothesis"]),
                         "status": outcomes[eid]["status"] if eid in outcomes else None,
                         "chk": verdicts.get(eid)}
                   for eid, reg in registrations.items()}

    nights = sorted(set(summaries_by_night) | set(reviews_by_night)
                    | {n["night"] for line in lines.values() for n in line["notes"]})
    night_rows = []
    for night in nights:
        night_summaries = summaries_by_night.get(night, [])
        conditions = [r["run_conditions"] for r in reviews_by_night.get(night, [])
                      if "run_conditions" in r]
        planned = any(n.get("planned") for line in lines.values()
                      for n in line["notes"] if n["night"] == night)
        if conditions:
            worst = max(conditions, key=lambda c: list(RUN).index(c["verdict"]))["verdict"]
            run, run_text = RUN[worst], worst
        elif night_summaries:
            run, run_text = "none", "not reviewed"
        else:
            run, run_text = "none", "planned" if planned else "no session"
        limits = [s["run_conditions"]["limits_hit"] for s in night_summaries
                  if s.get("run_conditions", {}).get("limits_hit", "none") != "none"]
        night_rows.append({
            "d": night, "label": label(night), "run": run, "runText": run_text,
            "fallback": any(s["instruction"].endswith("fallback.md") for s in night_summaries),
            "tip": " ".join(limits)})

    out_lines = []
    for line in tree_order(lines):
        mine = {eid for eid, lid in line_of.items() if lid == line["id"]}
        per_night: dict[str, dict] = {}
        for night in nights:
            notes = [n for n in line["notes"] if n["night"] == night]
            in_summary = {e["experiment_id"] for s in summaries_by_night.get(night, [])
                          for e in s["experiments"]}
            in_notes = {p["experiment_id"] for n in notes for p in n.get("experiments", [])}
            reviewed = reviewed_on.get(night, {})
            here = sorted(mine & (in_summary | in_notes | set(reviewed)))
            if not notes and not here:
                continue
            worst = max((reviewed[e] for e in here if e in reviewed),
                        key=VERDICT_RANK.get, default=None)
            if worst:
                kind = worst
            elif notes and all(n.get("planned") for n in notes):
                kind = "planned"
            elif any(n.get("waiting") for n in notes):
                kind = "waiting"
            elif notes and all(n["by"] == "human" for n in notes) and not here:
                kind = "decided"
            else:
                kind = "unreviewed"
            per_night[night] = {"k": kind, "e": here,
                                "notes": [{"t": n["text"], "by": who[n["by"]]} for n in notes]}
        seen = {e for cell in per_night.values() for e in cell["e"]}
        out_lines.append({
            "id": line["id"], "name": line.get("name", line["id"]), "depth": line["depth"],
            "parent": line.get("parent"), "by": who[line.get("by", "agent")],
            "opened": line.get("opened"), "status": line["status"],
            "feeds": [f for f in line["feeds"] if f in lines],
            "q": line.get("question", ""), "qsrc": line.get("question_source", ""),
            "reason": line.get("reason", ""), "state": line.get("state", ""),
            "nights": per_night, "arts": line["arts"], "other": sorted(mine - seen)})

    asks = None
    for night in sorted(summaries_by_night, reverse=True):
        items = []
        for summary in summaries_by_night[night]:
            review = next((r for r in reviews_by_night.get(night, [])
                           if r.get("line_id") == summary.get("line_id")), {})
            checked = review.get("answers", [])
            items += [{"q": a["asked"], "status": a["status"], "a": a["answer"],
                       "line": summary.get("line_id"),
                       "departures": a.get("departures", ""),
                       "verdict": checked[i]["verdict"] if i < len(checked) else None,
                       "finding": checked[i]["finding"] if i < len(checked) else ""}
                      for i, a in enumerate(summary.get("answers", []))]
        if items:
            asks = {"night": label(night), "items": items}
            break

    terms = [{"term": t["term"], "definition": t["definition"], "aliases": t.get("aliases", [])}
             for t in latest(records["terms.jsonl"], "term_id").values()]
    return {
        "name": program.name, "title": program.title, "scientist": program.scientist,
        "generated": f"{datetime.now():%b %-d, %Y at %H:%M}",
        "nights": night_rows, "lines": out_lines, "exps": experiments, "asks": asks,
        "terms": sorted(terms, key=lambda t: t["term"].lower()),
        "unassigned": sorted(set(registrations) - set(line_of))}


def render(model: dict) -> str:
    data = json.dumps(model, ensure_ascii=False).replace("</", "<\\/")
    return (PAGE.read_text()
            .replace("{{name}}", model["name"].capitalize())
            .replace("/*DATA*/null", data))


def write(program: Program, out: Path | None, nights: int) -> Path:
    model = build(program)
    model["mapNights"] = nights
    out = out or Path(program.data_dir).expanduser() / "map" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(model))
    return out
