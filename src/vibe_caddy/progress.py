"""Progress reporting for operations that are slow enough to narrate.

Logic modules never print. An operation that takes a while (setup, uninstall, init)
instead reports each step to an *observer* as it completes, and the presentation layer
decides how to show it. Defaulting to :func:`ignore` lets the dashboard and the tests
call the same code silently, with no flag to thread through.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Step:
    """One completed step: a short ``kind`` the renderer keys on, and its ``detail``."""

    kind: str
    detail: str


Observer = Callable[[Step], None]


def ignore(step: Step) -> None:
    """The default observer: report nothing."""
