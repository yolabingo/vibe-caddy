"""Exceptions that carry a message meant for the user's terminal."""

from __future__ import annotations


class VibeError(Exception):
    """Base class for failures vibe reports as a clean message, not a traceback.

    Attributes:
        hint: An optional follow-up line telling the user what to do next.
    """

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class NotFound(VibeError):
    """A named route does not exist in the registry."""


class Conflict(VibeError):
    """The requested change collides with existing state (a name, or a port)."""


class SetupRequired(VibeError):
    """vibe is not installed, or an external dependency is missing."""
