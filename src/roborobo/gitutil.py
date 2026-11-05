"""Small git helpers; every failure raises with git's own message."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def show(repo: Path, rev: str, path: str) -> bytes | None:
    done = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True)
    return done.stdout if done.returncode == 0 else None


def history(repo: Path, path: str) -> list[str]:
    """Commits that touched path, oldest first."""
    out = git(repo, "rev-list", "--reverse", "HEAD", "--", path, check=False)
    return [line for line in out.split() if line]


def has_head(repo: Path) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"],
                          capture_output=True).returncode == 0
