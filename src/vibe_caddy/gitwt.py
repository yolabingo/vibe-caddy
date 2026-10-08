"""Git worktree detection.

A linked worktree gets its own hostname so several branches of one app can run at
once without fighting over a port or a name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import names


def _git(directory: Path, *args: str) -> str | None:
    """Run a git command in ``directory``, returning stripped stdout or None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def main_checkout(directory: Path) -> Path | None:
    """Return the main checkout if ``directory`` is a linked worktree, else None.

    A linked worktree's ``--git-dir`` lives inside the main repo's ``worktrees/``
    subdirectory, so it differs from ``--git-common-dir``; in the main checkout the
    two are the same path.
    """
    git_dir = _git(directory, "rev-parse", "--absolute-git-dir")
    common = _git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not git_dir or not common:
        return None
    if Path(git_dir).resolve() == Path(common).resolve():
        return None
    # --git-common-dir points at the main checkout's `.git`; its parent is the tree.
    return Path(common).resolve().parent


def branch(directory: Path) -> str | None:
    """Return the current branch name, or None when detached or not a repo."""
    name = _git(directory, "rev-parse", "--abbrev-ref", "HEAD")
    return None if name in (None, "HEAD") else name


def slug_for(directory: Path, override: str | None = None) -> str:
    """Pick the hostname label for a worktree.

    Preference order is the explicit override, then the branch name, then the
    directory's own name -- the last covers detached HEAD checkouts.

    Raises:
        names.InvalidName: if nothing usable can be derived.
    """
    for candidate in (override, branch(directory), directory.name):
        if candidate:
            slug = names.slugify(candidate)
            if slug:
                return slug
    raise names.InvalidName(f"cannot derive a name from {directory}")


def linked_worktrees(main: Path) -> list[Path]:
    """List linked worktree directories of a main checkout, excluding the main one."""
    out = _git(main, "worktree", "list", "--porcelain")
    if not out:
        return []
    found = [
        Path(line.removeprefix("worktree ").strip())
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]
    return [path for path in found[1:] if path.exists()]
