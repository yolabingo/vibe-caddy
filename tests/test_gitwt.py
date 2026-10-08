"""Git worktree detection against a real temporary repository."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from vibe_caddy import gitwt, names

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def clean_git_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    main = tmp_path / "work" / "main-repo"
    main.mkdir(parents=True)
    git(main, "init", "-b", "main")
    git(main, "commit", "--allow-empty", "-m", "init")
    return main


@pytest.fixture
def linked(repo: Path) -> Path:
    path = repo.parent / "wt-dir"
    git(repo, "worktree", "add", "-b", "worktree-Feature/X", str(path))
    return path


def test_main_checkout_is_none_in_main_repo(repo: Path) -> None:
    assert gitwt.main_checkout(repo) is None


def test_main_checkout_from_linked_worktree(repo: Path, linked: Path) -> None:
    assert gitwt.main_checkout(linked) == repo.resolve()


def test_main_checkout_outside_any_repo(tmp_path: Path) -> None:
    assert gitwt.main_checkout(tmp_path / "work") is None


def test_branch(repo: Path, linked: Path, tmp_path: Path) -> None:
    assert gitwt.branch(repo) == "main"
    assert gitwt.branch(linked) == "worktree-Feature/X"
    assert gitwt.branch(tmp_path) is None


def test_branch_is_none_when_detached(repo: Path) -> None:
    git(repo, "checkout", "--detach")
    assert gitwt.branch(repo) is None


def test_slug_precedence(repo: Path, linked: Path, tmp_path: Path) -> None:
    assert gitwt.slug_for(linked, "My Override") == "my-override"
    assert gitwt.slug_for(linked) == "feature-x"  # branch, with worktree- prefix stripped
    git(linked, "checkout", "--detach")
    assert gitwt.slug_for(linked) == "wt-dir"  # falls back to the directory name


def test_slug_for_unusable_names_raises(tmp_path: Path) -> None:
    with pytest.raises(names.InvalidName):
        gitwt.slug_for(tmp_path / "!!!")


def test_linked_worktrees_excludes_main(repo: Path, linked: Path) -> None:
    assert [p.resolve() for p in gitwt.linked_worktrees(repo)] == [linked.resolve()]


def test_linked_worktrees_empty_without_worktrees(repo: Path) -> None:
    assert gitwt.linked_worktrees(repo) == []
