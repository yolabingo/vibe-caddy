"""Shared fixtures: every test runs against a throwaway home and a inert launchd.

``paths.home`` is the single seam all vibe-caddy paths derive from, so pointing it at
``tmp_path`` keeps ``~/.vibe-caddy`` and ``~/Library/LaunchAgents`` out of reach. Anything that
shells out to ``launchctl`` is replaced by a stub that reports "not loaded".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vibe_caddy import caddy, launchd, paths


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point vibe-caddy at ``tmp_path`` and neutralise ``launchctl``."""
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("SUDO_UID", raising=False)
    # paths resolves XDG base directories from the environment, so a developer
    # who redirects them would otherwise have tests write outside tmp_path.
    for variable in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(variable, raising=False)
    # Git hooks export repository paths that would redirect Git commands in
    # temporary projects back to this worktree instead of the test's checkout.
    for variable in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(paths, "home", lambda: tmp_path)

    def inert_run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["launchctl", *args], 1, "", "stubbed")

    monkeypatch.setattr(launchd, "_run", inert_run)
    return tmp_path


@pytest.fixture
def reloads(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace ``caddy.reload`` with a recorder; returns one entry per call.

    `reload` takes no snapshot -- it re-reads the registry under the publication
    lock -- so there is nothing to record but the call itself.
    """
    calls: list[object] = []

    def fake_reload(*, require_running: bool = True) -> None:
        calls.append(require_running)

    monkeypatch.setattr(caddy, "reload", fake_reload)
    return calls
