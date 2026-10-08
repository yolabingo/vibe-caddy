"""Structural tests: keep the presentation layers thin.

These parse source rather than run it, so they fail the moment a forbidden import is
added, whether or not anything exercises it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "vibe_caddy"
PRESENTATION = ("cli.py", "dashboard/app.py")

#: Modules a presentation layer must reach only through ``service`` or ``provision``.
LOW_LEVEL = ("registry", "launchd", "ports", "install", "frameworks", "project", "caddy")
PRESENTATION_LIBRARIES = ("typer", "rich")


def _imported(path: Path) -> set[str]:
    """Every module name a file imports, as the last component of its dotted path.

    ``from . import registry`` and ``from .. import registry`` yield ``registry``;
    ``from ..registry import load`` yields ``registry`` too; ``import typer`` yields
    ``typer``. Relative depth is ignored on purpose: the rule is about *which* module.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(
                    node.module.split(".")[0] if node.level == 0 else node.module.split(".")[-1]
                )
            else:
                found.update(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("module", PRESENTATION)
def test_presentation_layer_does_not_reach_low_level_modules(module: str) -> None:
    offenders = sorted(_imported(SRC / module) & set(LOW_LEVEL))
    assert not offenders, (
        f"{module} imports {offenders}. The CLI and the dashboard are presentation layers: "
        "they parse input, call ONE function in service.py or provision.py, and format "
        "the result. Orchestration and state access live below them so both front ends "
        "share it. Add the function you need to service.py (or provision.py for "
        "setup/uninstall/daemon control) and call that instead."
    )


def test_only_the_presentation_layers_import_typer_or_rich() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative == "cli.py" or relative.startswith("dashboard/"):
            continue
        used = _imported(path) & set(PRESENTATION_LIBRARIES)
        if used:
            offenders.append(f"{relative} imports {sorted(used)}")
    assert not offenders, (
        "; ".join(offenders) + ". typer and rich belong to the presentation layer "
        "(cli.py and dashboard/). Logic modules must not print or prompt: return data, "
        "or report progress through a vibe_caddy.progress observer, and let cli.py render it."
    )
