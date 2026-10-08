"""XDG base directory resolution.

The autouse ``isolated_home`` fixture clears every ``XDG_*`` variable, so these
tests set the ones they care about explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_caddy import paths


def test_defaults_follow_the_xdg_spec(tmp_path: Path) -> None:
    assert paths.data_dir() == tmp_path / ".local/share/vibe-caddy"
    assert paths.state_dir() == tmp_path / ".local/state/vibe-caddy"


def test_durable_files_live_in_data_and_derived_files_in_state(tmp_path: Path) -> None:
    assert paths.registry_file().parent == paths.data_dir()
    assert paths.caddy_data_dir().parent == paths.data_dir()
    for derived in (paths.caddyfile(), paths.launchd_dir(), paths.log_dir()):
        assert derived.parent == paths.state_dir()


@pytest.mark.parametrize(
    ("variable", "accessor"),
    [("XDG_DATA_HOME", paths.data_dir), ("XDG_STATE_HOME", paths.state_dir)],
)
def test_environment_overrides_are_honoured(
    variable: str, accessor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(variable, str(tmp_path / "custom"))
    assert accessor() == tmp_path / "custom" / "vibe-caddy"


@pytest.mark.parametrize("value", ["relative/path", ""])
def test_non_absolute_overrides_are_ignored(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The specification says a relative value must be treated as unset."""
    monkeypatch.setenv("XDG_DATA_HOME", value)
    assert paths.data_dir() == tmp_path / ".local/share/vibe-caddy"


def test_ensure_dirs_creates_both_trees(tmp_path: Path) -> None:
    paths.ensure_dirs()
    for directory in (paths.data_dir(), paths.state_dir(), paths.launchd_dir(), paths.log_dir()):
        assert directory.is_dir()


def test_nothing_is_written_outside_the_xdg_trees(tmp_path: Path) -> None:
    """A regression guard: no stray ``~/.vibe-caddy`` from the pre-XDG layout."""
    paths.ensure_dirs()
    assert not paths.legacy_dir().exists()
