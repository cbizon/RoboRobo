"""A branch and a worktree per line, for the service.

Each line runs in its own git worktree, ``<parent of the repo>/<program
folder>.<line>`` (``~/Projects/experiments/sequel.L0019``), on branch
``line/<line>``. The worktree sits beside the program's repository, so
relative paths such as uv's ``../research-programs`` still resolve, and the
program's data folder is shared.

Before a run, ``main`` is merged into the line's branch, so the line starts
from what other lines have merged. After a run that leaves its worktree clean
and ``program check`` passing, the line's branch is merged into ``main``:

- ledger files (append-only JSONL) merge with git's built-in ``union``
  driver (the template's ``.gitattributes``), so lines that appended records
  at the same time both keep them;
- any other conflict aborts the merge and pauses the line;
- the merged ``main`` must pass ``program check``; if it doesn't, the merge is
  undone (it was never pushed) and the line is paused.

The service pushes ``main``; line branches stay in the service's clone.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from roborobo import config
from roborobo.checks import check_program
from roborobo.config import Program
from roborobo.gitutil import git

LINE_ID = re.compile(r"L\d{4}")
# One thing at a time commits to, merges into or pushes the program's main
# clone: the service's own steps, a line's merge, an action from the dashboard.
MERGE_LOCK = threading.RLock()


def branch(line: str) -> str:
    return f"line/{line}"


def worktree_path(program: Program, line: str) -> Path:
    if not LINE_ID.fullmatch(line):
        raise ValueError(f"{line!r} isn't a line id")
    return program.repo.parent / f"{program.repo.name}.{line}"


def main_branch(program: Program) -> str:
    return git(program.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def worktree(program: Program, line: str) -> Program:
    """The line's worktree as a program, created from main the first time."""
    path = worktree_path(program, line)
    if not (path / ".git").exists():
        exists = git(program.repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch(line)}",
                     check=False).strip()
        if exists:
            git(program.repo, "worktree", "add", "-q", str(path), branch(line))
        else:
            git(program.repo, "worktree", "add", "-q", "-b", branch(line), str(path),
                main_branch(program))
    return config.load(path)


def dirty(repo: Path) -> list[str]:
    return [line for line in git(repo, "status", "--porcelain").splitlines() if line]


def start(program: Program, line: str) -> tuple[Program | None, str | None]:
    """The line's worktree, brought up to date with main. Returns it, or why
    the line can't start (to pause it)."""
    tree = worktree(program, line)
    left = dirty(tree.repo)
    if left:
        return None, f"{line}'s worktree has uncommitted changes: " + "; ".join(left[:5])
    main = main_branch(program)
    out = git(tree.repo, "merge", "-q", "--no-edit", main, check=False)
    if dirty(tree.repo):
        git(tree.repo, "merge", "--abort", check=False)
        return None, f"merging {main} into {branch(line)} conflicts ({out.strip()[:200]})"
    return tree, None


def refresh(program: Program, line: str) -> str | None:
    """Merge main into the line's branch again, before a check: main may have
    moved during the run (a new approval, another line's merge), and the check
    in the worktree must see what the check on main will. Returns a problem,
    or None; an unclean worktree is left for the check to report."""
    tree = worktree_path(program, line)
    if dirty(tree):
        return None
    with MERGE_LOCK:
        main = main_branch(program)
    out = git(tree, "merge", "-q", "--no-edit", main, check=False)
    if dirty(tree):
        git(tree, "merge", "--abort", check=False)
        return f"merging {main} into {branch(line)} conflicts ({out.strip()[:200]})"
    return None


def unmerged(program: Program) -> list[str]:
    """Lines whose branch has commits main doesn't: finished work that was
    never merged (a run paused after it, or the service stopped in between)."""
    main = main_branch(program)
    found = []
    refs = git(program.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/line/")
    for ref in refs.split():
        if int(git(program.repo, "rev-list", "--count", f"{main}..{ref}").strip()):
            found.append(ref.removeprefix("line/"))
    return found


def finish(program: Program, line: str) -> str | None:
    """Merge the line's branch into main and check the result. Returns why it
    couldn't (the line is then paused), or None."""
    with MERGE_LOCK:
        if dirty(program.repo):
            return f"the program's clone has uncommitted changes: {'; '.join(dirty(program.repo)[:5])}"
        before = git(program.repo, "rev-parse", "HEAD").strip()
        out = git(program.repo, "merge", "-q", "--no-edit", branch(line), check=False)
        if dirty(program.repo):
            git(program.repo, "merge", "--abort", check=False)
            return f"merging {branch(line)} into {main_branch(program)} conflicts ({out.strip()[:200]})"
        failed, _ = check_program(config.load(program.repo))
        if failed:
            git(program.repo, "reset", "-q", "--hard", before)  # never pushed: undo the merge
            return (f"{main_branch(program)} with {branch(line)} merged fails program check, so the "
                    "merge was undone: " + "; ".join(failed[:3]))
    return None
