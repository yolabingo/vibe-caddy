"""Route-name rules and slugification.

A route name is a DNS label, optionally prefixed with one more label to express a
git worktree (``<worktree>.<app>``). Exactly one dot is allowed: deeper nesting has
no meaning to vibe and would make the parent lookup ambiguous.
"""

from __future__ import annotations

import re

from .errors import VibeError

LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

#: Names vibe serves itself and therefore refuses to hand out.
RESERVED = frozenset({"local", "localhost"})

MAX_LABEL_LEN = 63


class InvalidName(VibeError, ValueError):
    """Raised when a route name cannot be used as a hostname label.

    Subclasses ``ValueError`` as well so pydantic validators treat it as a
    validation failure rather than letting it escape as an internal error.
    """


def validate(name: str) -> str:
    """Normalise and validate a route name, returning the canonical form.

    Raises:
        InvalidName: if the name is empty, reserved, too deeply nested, or
            contains characters that are not valid in a DNS label.
    """
    canonical = name.strip().lower()
    if not canonical:
        raise InvalidName("name is empty")

    labels = canonical.split(".")
    if len(labels) > 2:
        raise InvalidName(
            f"{name!r} has {len(labels) - 1} dots; at most one is allowed (<worktree>.<app>)"
        )

    for label in labels:
        if not label:
            raise InvalidName(f"{name!r} has an empty label")
        if len(label) > MAX_LABEL_LEN:
            raise InvalidName(f"label {label!r} exceeds {MAX_LABEL_LEN} characters")
        if not LABEL_RE.match(label):
            raise InvalidName(
                f"label {label!r} must be lowercase alphanumeric with internal hyphens only"
            )

    if canonical in RESERVED:
        raise InvalidName(f"{name!r} is reserved")
    return canonical


def parent_of(name: str) -> str | None:
    """Return the app name a worktree route hangs off, or ``None`` if it is a root."""
    app = name.partition(".")[2]
    return app or None


def slugify(value: str) -> str:
    """Reduce a branch or directory name to a usable DNS label.

    The ``worktree-`` prefix is stripped because agent harnesses commonly create
    branches named ``worktree-<topic>``, and repeating it in every hostname adds
    nothing.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"^worktree-", "", slug).strip("-")
    return slug[:MAX_LABEL_LEN].strip("-")
