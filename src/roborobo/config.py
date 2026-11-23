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
    "scheduled/line.md",
    "scheduled/line-checker.md",
    "scheduled/debrief.md",
    "scheduled/run-night.plist",
    "evidence/README.md",
    "instructions/README.md",
    "ledger/README.md",
    "lines/README.md",
    "reviews/README.md",
    "sessions/README.md",
)

# Template-owned files RoboRobo no longer renders; `program sync` removes them.
RETIRED = (
    "scheduled/session.md",
    "scheduled/checker.md",
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
    "ledger/experiments.jsonl",
    "ledger/claims.jsonl",
    "ledger/decisions.jsonl",
    "ledger/blockers.jsonl",
    "ledger/lines.jsonl",
    "ledger/terms.jsonl",
    "ledger/artifacts.jsonl",
)

DEFAULT_HUMAN_ONLY = ["DIRECTION.md", "BRIEF.md", "budgets.yaml", CONFIG, "referee/**",
                      "notes/**"]

LEDGER = {
    "experiments.jsonl": ("experiment.schema.json", "experiment_id", "E"),
    "claims.jsonl": ("claim.schema.json", "claim_id", "C"),
    "decisions.jsonl": ("decision.schema.json", "decision_id", "D"),
    "blockers.jsonl": ("blocker.schema.json", "blocker_id", "B"),
    "lines.jsonl": ("line.schema.json", "line_id", "L"),
    "terms.jsonl": ("term.schema.json", "term_id", "T"),
    "artifacts.jsonl": ("artifact.schema.json", "artifact_id", "A"),
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
    runs: dict = field(default_factory=lambda: dict(DEFAULT_RUNS))
    # Clusters jobs may run on ([hosts.<name>]); see roborobo.cluster.
    hosts: dict = field(default_factory=dict)
    # How the always-on service runs this program ([service]); see roborobo.service.
    service: dict = field(default_factory=lambda: dict(DEFAULT_SERVICE))

    def values(self, roborobo: Path) -> dict[str, str]:
        """Placeholder values for rendering the template."""
        start_hour, start_minute = self.schedule["session_start"].split(":")
        return {
            "session_start_hour": str(int(start_hour)),
            "session_start_minute": str(int(start_minute)),
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


SCHEDULE_KEYS = ("session_start", "session_end", "checker_minutes", "debrief")

# How `program run-night` runs each line: the agent command (the prompt is
# appended as its last argument) and the time limit of one line's worker
# run, in minutes.  The checker's limit is [schedule] checker_minutes.
DEFAULT_RUNS = {"agent": ["claude", "-p"], "worker_minutes": 60}

# Limits on the always-on service, per program: runs of one line in a day,
# and the least time between the starts of one line's runs.  Every finished
# job is an event, so without them a busy cluster would mean a run per job.
DEFAULT_SERVICE = {"max_runs_per_line_per_day": 3, "min_minutes_between_runs": 60}


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
        runs=DEFAULT_RUNS | data.get("runs", {}),
        hosts=data.get("hosts", {}),
        service=DEFAULT_SERVICE | data.get("service", {}),
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
        "# Clusters the agent may submit jobs to with `program submit`. For example:",
        "# [hosts.hatteras]",
        '# kind = "slurm"',
        '# ssh = "hatteras"                # an alias in ~/.ssh/config',
        '# workdir = "/projects/<alloc>/roborobo"',
        "# slots = 8                       # most jobs queued or running at once",
        '# partition = "batch"',
        '# time = "2:00:00"                # defaults; `program submit` can override',
        '# mem = "16G"',
        "# cpus = 1",
        '# fetch = ["*.json", "*.txt", "*.log", "*.csv", "*.md"]  # small files copied back',
        "# fetch_max_mb = 500              # cap on those, and on `program fetch`",
        '# env = {UV_CACHE_DIR = "/projects/<alloc>/uv-cache", UV_LINK_MODE = "copy"}',
        "",
    ]
    return "\n".join(lines)
