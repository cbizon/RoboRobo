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
    """Commits on the current branch's first-parent line that touched path,
    oldest first. Following first parents, a merged line's records arrive in
    the merge commit, after the branch's own earlier versions."""
    out = git(repo, "rev-list", "--first-parent", "--reverse", "HEAD", "--", path, check=False)
    return [line for line in out.split() if line]


def committed_at(repo: Path, path: str) -> int:
    """When a path was last committed (Unix seconds), or 0 if it never was."""
    out = git(repo, "log", "-1", "--format=%ct", "--", path, check=False).strip()
    return int(out) if out else 0


def has_head(repo: Path) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"],
                          capture_output=True).returncode == 0
