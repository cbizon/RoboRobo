"""The scientist's approvals of human-only files, anchored in git.

``program approve`` records a SHA-256 manifest of every human-only file in
``approvals.json``, commits it, tags the commit ``approved/<UTC time>``, and
pushes the tag to the remote.  ``program check`` fetches the approval tags
from the remote and compares the working tree with the latest one.

Why the remote: on a laptop the agent runs as the scientist, so it could
rewrite a local ``approvals.json`` along with the files it protects.  The
pushed tag is a record the agent can't quietly change (its settings deny
``git push`` and ``git tag``), and ``program check`` lists every approval
made in the last day, so an approval the scientist didn't make shows up in
the morning.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from roborobo.config import APPROVALS, Program
from roborobo.gitutil import git, show

TAG_PREFIX = "approved/"
TAG_FORMAT = "%Y%m%dT%H%M%S%fZ"     # microseconds: two approvals can't collide


def human_files(program: Program) -> list[str]:
    files = []
    for path in sorted(program.repo.rglob("*")):
        rel = path.relative_to(program.repo).as_posix()
        if path.is_file() and not rel.startswith(".git/") and program.is_human_only(rel):
            files.append(rel)
    return files


def manifest(program: Program) -> dict[str, str]:
    return {rel: hashlib.sha256((program.repo / rel).read_bytes()).hexdigest()
            for rel in human_files(program)}


def approve(program: Program, remote: str = "origin", now: datetime | None = None) -> str:
    files = human_files(program)
    dirty = git(program.repo, "status", "--porcelain", "--", *files).strip()
    if dirty:
        raise RuntimeError("commit your changes to human-only files before approving:\n" + dirty)
    now = now or datetime.now(UTC)
    by = git(program.repo, "config", "user.name").strip() or "unknown"
    record = {"approved_at": now.isoformat(timespec="seconds"), "by": by,
              "files": manifest(program)}
    (program.repo / APPROVALS).write_text(json.dumps(record, indent=1) + "\n")
    git(program.repo, "add", APPROVALS)
    git(program.repo, "commit", "-q", "-m", f"Approve human-only files ({len(files)})",
        "--", APPROVALS)
    tag = TAG_PREFIX + now.strftime(TAG_FORMAT)
    git(program.repo, "tag", tag)
    git(program.repo, "push", "-q", remote, "HEAD", f"refs/tags/{tag}")
    return tag


def tag_time(tag: str) -> datetime:
    stamp = tag[len(TAG_PREFIX):]
    for fmt in (TAG_FORMAT, "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"approval tag {tag} has no readable time")


def remote_approvals(program: Program, remote: str = "origin") -> list[str]:
    """Approval tags on the remote, oldest first; local tags are replaced by
    the remote's so a locally forged or moved tag doesn't count."""
    git(program.repo, "fetch", "-q", "--force", remote,
        f"+refs/tags/{TAG_PREFIX}*:refs/tags/{TAG_PREFIX}*")
    remote_tags = {line.split("refs/tags/")[1]
                   for line in git(program.repo, "ls-remote", "--tags", remote,
                                   f"refs/tags/{TAG_PREFIX}*").splitlines()}
    return sorted(remote_tags)


def verify(program: Program, remote: str = "origin") -> tuple[list[str], dict[str, object]]:
    """Problems with human-only files, and facts to report."""
    tags = remote_approvals(program, remote)
    if not tags:
        return ([f"no approval on {remote}: run `program approve` after reviewing "
                 "the human-only files"], {})
    latest = tags[-1]
    raw = show(program.repo, latest, APPROVALS)
    if raw is None:
        return [f"approval {latest} has no {APPROVALS}"], {}
    approved = json.loads(raw)["files"]
    current = manifest(program)
    problems = []
    for rel in sorted(approved.keys() | current.keys()):
        if rel not in current:
            problems.append(f"{rel} was approved in {latest} but is missing")
        elif rel not in approved:
            problems.append(f"{rel} is human-only but not in approval {latest}")
        elif approved[rel] != current[rel]:
            problems.append(f"{rel} changed since approval {latest}")
    day_ago = datetime.now(UTC) - timedelta(days=1)
    recent = [tag for tag in tags if tag_time(tag) > day_ago]
    return problems, {"latest_approval": latest, "approvals_last_day": recent}
