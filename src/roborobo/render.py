"""Rendering the template into a program repository."""

from __future__ import annotations

import json
import re
from pathlib import Path

from roborobo import ROOT, TEMPLATE
from roborobo.config import APPROVALS, CONFIG, SEEDED, TEMPLATE_OWNED, Program

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


def fill(text: str, values: dict[str, str], source: str) -> str:
    unknown = sorted({m.group(1) for m in PLACEHOLDER.finditer(text)} - values.keys())
    if unknown:
        raise KeyError(f"{source}: no value for {', '.join(unknown)}")
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def settings(program: Program) -> str:
    """Claude Code permissions: the agent's file tools can't touch files it
    doesn't own, and it can't push, tag, or approve.  These deny rules stop
    honest mistakes; `program check` is what catches the rest (a shell
    command can still write anywhere)."""
    protected = [*program.human_only, APPROVALS,
                 *(p for p in TEMPLATE_OWNED if p != ".claude/settings.json")]
    deny = [f"{tool}({path})" for path in protected for tool in ("Edit", "Write")]
    deny += ["Edit(.claude/**)", "Write(.claude/**)",
             "Bash(git push:*)", "Bash(git tag:*)", "Bash(git commit --amend:*)",
             "Bash(git rebase:*)", "Bash(git reset --hard:*)",
             "Bash(program approve:*)", "Bash(uv run program approve:*)"]
    return json.dumps({"permissions": {"deny": deny}}, indent=2) + "\n"


def rendered(program: Program, path: str) -> str:
    """The text a template-owned or seeded file should have."""
    if path == ".claude/settings.json":
        return settings(program)
    source = TEMPLATE / path
    return fill(source.read_text(), program.values(ROOT), str(source))


def write_template_owned(program: Program) -> list[str]:
    """Render every template-owned file; return those that changed."""
    changed = []
    for path in TEMPLATE_OWNED:
        target = program.repo / path
        text = rendered(program, path)
        if not target.exists() or target.read_text() != text:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            changed.append(path)
    return changed


def write_missing_seeded(program: Program) -> list[str]:
    """Create seeded files that don't exist yet; never overwrite one."""
    created = []
    for path in SEEDED:
        target = program.repo / path
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered(program, path))
            created.append(path)
    return created


def template_drift(program: Program) -> list[str]:
    """Template-owned files that differ from their rendering."""
    return [path for path in TEMPLATE_OWNED
            if not (program.repo / path).exists()
            or (program.repo / path).read_text() != rendered(program, path)]


__all__ = ["CONFIG", "fill", "rendered", "settings", "template_drift",
           "write_missing_seeded", "write_template_owned"]
