"""The scientist's approvals of human-only files, anchored in git and signed.

``program approve`` records a SHA-256 manifest of every human-only file in
``approvals.json``, commits it, makes an annotated tag ``approved/<UTC
time>`` signed with the scientist's SSH key (git's ``user.signingkey``,
``gpg.format = ssh``), and pushes it.  ``program check`` fetches the
approval tags from the remote, requires the latest to carry a good
signature from a key in RoboRobo's ``approvers`` file, and compares the
working tree with it.

Why signed: anything that can push to the program's repository can push a
tag, and the service's deploy key acts as the repository's owner, so GitHub
rules don't stop it (on Nov 23 an agent run in the service pod approved its
own change this way). A signature needs the scientist's private key, which
lives only on the scientist's computer, and the list of keys that count
lives in RoboRobo, which the service can't push to.  ``program check`` also
lists every approval made in the last day, so an unexpected one is seen.

Human-only files are the ones git tracks or would track: git-ignored files
(caches, build output) never count, so running the referee's tests can't
make an approval look stale.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from roborobo import ROOT
from roborobo.config import APPROVALS, Program
from roborobo.gitutil import git, show

# The SSH public keys whose signatures make an approval, one per line in
# ssh's allowed-signers format: "<email> <key type> <key>".
APPROVERS = ROOT / "approvers"
TAG_PREFIX = "approved/"
TAG_FORMAT = "%Y%m%dT%H%M%S%fZ"     # microseconds: two approvals can't collide


def human_files(program: Program) -> list[str]:
    """Human-only files git tracks or would track; ignored files never count."""
    listed = git(program.repo, "ls-files", "--cached", "--others", "--exclude-standard",
                 "-z").split("\0")
    return sorted({rel for rel in listed if rel and (program.repo / rel).is_file()
                   and program.is_human_only(rel)})


def manifest(program: Program) -> dict[str, str]:
    return {rel: hashlib.sha256((program.repo / rel).read_bytes()).hexdigest()
            for rel in human_files(program)}


def signing_key(program: Program) -> str:
    """The public key git will sign with; fails, saying how to set one up, if
    there is none or it isn't one of RoboRobo's approvers."""
    key = git(program.repo, "config", "user.signingkey", check=False).strip()
    fmt = git(program.repo, "config", "gpg.format", check=False).strip()
    if not key or fmt != "ssh":
        raise SystemExit("approvals are signed with your SSH key: set it up once with\n"
                         "  git config --global gpg.format ssh\n"
                         "  git config --global user.signingkey ~/.ssh/<your key>.pub\n"
                         f"and add the public key to {APPROVERS}")
    if key.startswith("key::"):  # the key itself
        public = key[len("key::"):]
    else:  # a key file; for a private key, its .pub beside it
        path = Path(key).expanduser()
        path = path if path.name.endswith(".pub") else Path(f"{path}.pub")
        if not path.exists():
            raise SystemExit(f"user.signingkey names {key}, but {path} doesn't exist")
        public = path.read_text()
    words = public.split()
    if len(words) < 2:
        raise SystemExit(f"can't read a public key from user.signingkey ({key})")
    listed = APPROVERS.read_text() if APPROVERS.exists() else ""
    if f"{words[0]} {words[1]}" not in listed:
        raise SystemExit(f"your signing key isn't in {APPROVERS}; add a line\n"
                         f"  <your email> {words[0]} {words[1]}\n"
                         "to RoboRobo, merge it, then approve")
    return f"{words[0]} {words[1]}"


def approve(program: Program, remote: str = "origin", now: datetime | None = None) -> str:
    signing_key(program)
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
    git(program.repo, "tag", "-s", "-m", f"Approval of {len(files)} human-only files by {by}",
        tag)
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
    remote_tags = {line.split("refs/tags/")[1].removesuffix("^{}")  # ^{}: the tagged commit
                   for line in git(program.repo, "ls-remote", "--tags", remote,
                                   f"refs/tags/{TAG_PREFIX}*").splitlines()}
    return sorted(remote_tags)


def signature_problem(program: Program, tag: str) -> str | None:
    """Why an approval tag doesn't count, or None if it carries a good
    signature from one of RoboRobo's approvers."""
    if not APPROVERS.exists() or not APPROVERS.read_text().strip():
        return f"{APPROVERS} lists no approvers, so no approval can count; add your SSH key"
    done = subprocess.run(["git", "-C", str(program.repo),
                           "-c", f"gpg.ssh.allowedSignersFile={APPROVERS}",
                           "verify-tag", tag], capture_output=True, text=True)
    if done.returncode != 0:
        detail = (done.stderr.strip().splitlines() or ["no signature"])[-1]
        return (f"approval {tag} isn't signed by an approver in {APPROVERS} ({detail}); "
                "only an approval you signed counts: review the human-only files and run "
                "`program approve`")
    return None


def verify(program: Program, remote: str = "origin") -> tuple[list[str], dict[str, object]]:
    """Problems with human-only files, and facts to report."""
    tags = remote_approvals(program, remote)
    if not tags:
        return ([f"no approval on {remote}: run `program approve` after reviewing "
                 "the human-only files"], {})
    latest = tags[-1]
    unsigned = signature_problem(program, latest)
    if unsigned:
        return [unsigned], {"latest_approval": latest, "approvals_last_day": []}
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
