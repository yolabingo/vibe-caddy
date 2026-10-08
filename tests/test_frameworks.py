"""Framework presets, detection, and the contract every preset must satisfy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibe_caddy import frameworks
from vibe_caddy.errors import VibeError


def test_registry_is_populated() -> None:
    assert frameworks.REGISTRY


@pytest.mark.parametrize("preset", list(frameworks.REGISTRY.values()), ids=lambda p: p.name)
def test_every_preset_accounts_for_the_assigned_port(preset: frameworks.Framework) -> None:
    """The whole purpose of a preset: the port vibe assigns must reach the server.

    Either the command passes it, or the preset declares that the application
    reads PORT from the environment. A preset doing neither would bind the
    framework's own default while the proxy points elsewhere, which is exactly
    the failure these presets exist to prevent.
    """
    assert ("$PORT" in preset.cmd) ^ preset.reads_port_env


@pytest.mark.parametrize(
    "preset",
    [f for f in frameworks.REGISTRY.values() if f.reads_port_env],
    ids=lambda p: p.name,
)
def test_env_port_presets_warn_the_user(preset: frameworks.Framework) -> None:
    """These are the ones that can bind the wrong port silently, so they must say so."""
    assert any("PORT" in note for note in preset.notes)


@pytest.mark.parametrize("preset", list(frameworks.REGISTRY.values()), ids=lambda p: p.name)
def test_every_preset_is_self_consistent(preset: frameworks.Framework) -> None:
    assert preset.name == preset.name.lower()
    assert preset.label
    # A preset nothing can detect is still usable by name, but one with content
    # rules and no files to read them from can never match.
    if preset.detect_contains:
        assert preset.detect_files


@pytest.mark.parametrize("preset", list(frameworks.REGISTRY.values()), ids=lambda p: p.name)
def test_no_preset_enables_a_reloader_that_reexecs(preset: frameworks.Framework) -> None:
    """launchd tracks the PID it spawned; a reloader that re-execs hides the server."""
    assert "--reload " not in f"{preset.cmd} "
    assert not preset.cmd.endswith("--reload")


def test_get_is_case_insensitive() -> None:
    assert frameworks.get("VITE").name == "vite"


def test_get_rejects_unknown_names() -> None:
    with pytest.raises(VibeError, match="unknown framework"):
        frameworks.get("not-a-framework")


# ------------------------------------------------------------------ detection


def write_package_json(root: Path, **sections: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({"name": root.name, **sections}))


def write_pyproject(root: Path, dependencies: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    body = ", ".join(f'"{entry}"' for entry in dependencies)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "p"\nversion = "0"\ndependencies = [{body}]\n'
    )


def test_detects_npm_dependency(tmp_path: Path) -> None:
    write_package_json(tmp_path, devDependencies={"vite": "^7.1.3"})
    found = frameworks.detect(tmp_path)
    assert found is not None
    assert found.name == "vite"


def test_detects_python_dependency(tmp_path: Path) -> None:
    write_pyproject(tmp_path, ["fastapi>=0.143", "uvicorn"])
    found = frameworks.detect(tmp_path)
    assert found is not None
    # FastAPI outranks the bare uvicorn preset it depends on.
    assert found.name == "fastapi"


def test_detects_django_by_marker_file_content(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python\nimport django\n")
    found = frameworks.detect(tmp_path)
    assert found is not None
    assert found.name == "django"


def test_marker_file_without_the_content_marker_does_not_match(tmp_path: Path) -> None:
    """A manage.py that is not Django's must not be claimed."""
    (tmp_path / "manage.py").write_text("print('some other project')\n")
    assert frameworks.detect(tmp_path) is None


def test_returns_none_for_an_unrecognised_project(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("nothing to see")
    assert frameworks.detect(tmp_path) is None


def test_malformed_manifests_are_ignored_rather_than_raising(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not json")
    (tmp_path / "pyproject.toml").write_text("[project\nbroken")
    assert frameworks.detect(tmp_path) is None


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("django", "django"),
        ("Django>=5.0", "django"),
        ("fastapi[standard]>=0.143", "fastapi"),
        ("flask ; python_version>'3.8'", "flask"),
        ("  Streamlit == 1.2  ", "streamlit"),
    ],
)
def test_requirement_strings_reduce_to_distribution_names(requirement: str, expected: str) -> None:
    assert frameworks._distribution_name(requirement) == expected


def test_higher_priority_wins_when_several_match(tmp_path: Path) -> None:
    """A Next.js project also carries Vite-adjacent tooling; the app framework wins."""
    write_package_json(tmp_path, dependencies={"next": "15.0.0", "vite": "^7.0.0"})
    found = frameworks.detect(tmp_path)
    assert found is not None
    assert found.name == "next"
