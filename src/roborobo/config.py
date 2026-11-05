"""A program's configuration (``program.toml``) and who owns which files.

Every file in a program repository has one owner:

- **template**: rendered from RoboRobo's ``template/`` with the program's
  values; ``program check`` fails if one differs from its rendering, and
  ``program sync`` re-renders them after RoboRobo changes;
- **human**: the scientist's (``files.human_only`` globs); fingerprinted by
  ``program approve``, and ``program check`` fails if one changed since;
- **agent**: everything else (``PROGRAM.md``, ``ledger/``, ``sessions/``,
  ``evidence/``, ``instructions/``, ``reviews/``), validated by
  ``program check`` where a schema applies.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG = "program.toml"
APPROVALS = "approvals.json"

# Rendered from template/ and kept identical to the rendering.
TEMPLATE_OWNED = (
    "CLAUDE.md",
    "CHECKER.md",
    ".claude/settings.json",
    "scheduled/README.md",
    "scheduled/session.md",
    "scheduled/checker.md",
    "scheduled/debrief.md",
    "evidence/README.md",
    "instructions/README.md",
    "ledger/README.md",
    "reviews/README.md",
    "sessions/README.md",
)

# Copied from template/ once, by `program new`, then owned by someone else.
SEEDED = (
    "DIRECTION.md",
    "BRIEF.md",
    "PROGRAM.md",
    "budgets.yaml",
    "referee/README.md",
    "referee/checker.md",
    "notes/README.md",
    "instructions/fallback.md",
    "ledger/experiments.jsonl",
    "ledger/claims.jsonl",
    "ledger/decisions.jsonl",
    "ledger/blockers.jsonl",
)

DEFAULT_HUMAN_ONLY = ["DIRECTION.md", "BRIEF.md", "budgets.yaml", CONFIG, "referee/**",
                      "notes/**"]

LEDGER = {
    "experiments.jsonl": ("experiment.schema.json", "experiment_id", "E"),
    "claims.jsonl": ("claim.schema.json", "claim_id", "C"),
    "decisions.jsonl": ("decision.schema.json", "decision_id", "D"),
    "blockers.jsonl": ("blocker.schema.json", "blocker_id", "B"),
}


@dataclass
class Program:
    repo: Path
    name: str
    title: str
    scientist: str
    data_dir: str
    timezone: str
    schedule: dict[str, str]
    human_only: list[str] = field(default_factory=lambda: list(DEFAULT_HUMAN_ONLY))

    def values(self, roborobo: Path) -> dict[str, str]:
        """Placeholder values for rendering the template."""
        return {
            "name": self.name,
            "title": self.title,
            "scientist": self.scientist,
            "repo": tilde(self.repo),
            "data_dir": self.data_dir,
            "timezone": self.timezone,
            "roborobo": tilde(roborobo),
            "schemas": tilde(roborobo / "schemas"),
            **{key: str(value) for key, value in self.schedule.items()},
        }

    def is_human_only(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) or
                   (pattern.endswith("/**") and path.startswith(pattern[:-2]))
                   for pattern in self.human_only)


def tilde(path: Path) -> str:
    path = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    return "~/" + str(path.relative_to(home)) if path.is_relative_to(home) else str(path)


SCHEDULE_KEYS = ("session_start", "session_end", "checker", "checker_minutes", "debrief")


def load(repo: Path) -> Program:
    repo = Path(repo).expanduser().resolve()
    path = repo / CONFIG
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; is {repo} a program repository?")
    data = tomllib.loads(path.read_text())
    program = data["program"]
    schedule = data["schedule"]
    missing = [key for key in SCHEDULE_KEYS if key not in schedule]
    if missing:
        raise ValueError(f"{path}: [schedule] is missing {', '.join(missing)}")
    return Program(
        repo=repo,
        name=program["name"],
        title=program["title"],
        scientist=program["scientist"],
        data_dir=program["data_dir"],
        timezone=program.get("timezone", "America/New_York"),
        schedule=schedule,
        human_only=data.get("files", {}).get("human_only", list(DEFAULT_HUMAN_ONLY)),
    )


def render_config(name: str, title: str, scientist: str, data_dir: str,
                  schedule: dict[str, str], timezone: str) -> str:
    human = ", ".join(f'"{p}"' for p in DEFAULT_HUMAN_ONLY)
    lines = [
        "# Written by `program new`; human-only. After editing, run `program sync`",
        "# and `program approve`.",
        "[program]",
        f'name = "{name}"',
        f'title = "{title}"',
        f'scientist = "{scientist}"',
        f'data_dir = "{data_dir}"',
        f'timezone = "{timezone}"',
        "",
        "[schedule]",
        *(f'{key} = "{schedule[key]}"' for key in SCHEDULE_KEYS),
        "",
        "[files]",
        f"human_only = [{human}]",
        "",
    ]
    return "\n".join(lines)
