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
            "placed": {}, "notes": []})
        if record["record"] == "open":
            line["by"] = record["by"]
            line["opened"] = record.get("night", record["at"][:10])
        for key in ("name", "question", "question_source", "reason", "parent",
                    "status", "state", "feeds"):
            if key in record and record["record"] != "note":
                line[key] = record[key]
        for artifact in record.get("artifacts", []):
            pair = [artifact["kind"], artifact["path"]]
            if pair not in line["arts"]:
                line["arts"].append(pair)
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
    summary_by_night = {s["date"]: s for _, s in summaries}
    review_by_night = {r["date"]: r for _, r in reviews}
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
    for night in sorted(review_by_night):
        for item in review_by_night[night]["reviews"]:
            verdicts[item["claim_id"]] = item["verdict"]
            reviewed_on.setdefault(night, {})[item["claim_id"]] = item["verdict"]

    experiments = {eid: {"title": titles.get(eid) or short(reg["hypothesis"]),
                         "status": outcomes[eid]["status"] if eid in outcomes else None,
                         "chk": verdicts.get(eid)}
                   for eid, reg in registrations.items()}

    nights = sorted(set(summary_by_night) | set(review_by_night)
                    | {n["night"] for line in lines.values() for n in line["notes"]})
    night_rows = []
    for night in nights:
        summary, review = summary_by_night.get(night), review_by_night.get(night)
        conditions = (review or {}).get("run_conditions")
        planned = any(n.get("planned") for line in lines.values()
                      for n in line["notes"] if n["night"] == night)
        if conditions:
            run, run_text = RUN[conditions["verdict"]], conditions["verdict"]
        elif summary:
            run, run_text = "none", "not reviewed"
        else:
            run, run_text = "none", "planned" if planned else "no session"
        instruction = (summary or {}).get("instruction", "")
        night_rows.append({
            "d": night, "label": label(night), "run": run, "runText": run_text,
            "fallback": instruction.endswith("fallback.md"),
            "tip": ((summary or {}).get("run_conditions") or {}).get("limits_hit", "")})

    out_lines = []
    for line in tree_order(lines):
        mine = {eid for eid, lid in line_of.items() if lid == line["id"]}
        per_night: dict[str, dict] = {}
        for night in nights:
            notes = [n for n in line["notes"] if n["night"] == night]
            in_summary = {e["experiment_id"]
                          for e in summary_by_night.get(night, {}).get("experiments", [])}
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
    for night in sorted(summary_by_night, reverse=True):
        answers = summary_by_night[night].get("answers")
        if answers:
            checked = review_by_night.get(night, {}).get("answers", [])
            asks = {"night": label(night), "items": [
                {"q": a["asked"], "status": a["status"], "a": a["answer"],
                 "departures": a.get("departures", ""),
                 "verdict": checked[i]["verdict"] if i < len(checked) else None,
                 "finding": checked[i]["finding"] if i < len(checked) else ""}
                for i, a in enumerate(answers)]}
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
