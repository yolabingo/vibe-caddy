"""Discovery and parsing of a project's ``vibe.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path

from .errors import VibeError
from .models import ProjectConfig

CONFIG_NAME = "vibe-caddy.toml"

#: What the file was called before the project was renamed. Found copies are
#: reported rather than read: silently honouring both names would leave two
#: files that can disagree.
LEGACY_CONFIG_NAME = "vibe.toml"

TEMPLATE = """\
# vibe-caddy project config -- https://<name>.vc.localhost
name = {name}

# Shell command that starts the dev server. It MUST bind the port vibe-caddy
# assigns, which arrives in the environment as $PORT. A command that ignores
# $PORT binds the framework's own default while the proxy points at the assigned
# one, and the route then answers nothing.
# `vibe-caddy init --framework list` shows a known-good command per framework.
cmd = {cmd}

# Fixed port instead of an auto-assigned one. Omit for auto-assignment.
# port = 3000

# Emoji or image URL shown on the dashboard.
# icon = {icon}

# Start this app at login as well as on `vibe-caddy start`.
# autostart = false

# Extra ports the command binds, exported as $PORT_<UPPERCASE_KEY>.
# Give a number to pin one, or 0 to have vibe-caddy assign it.
# [reserve_ports]
# websocket = 0
"""


_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_string(value: str) -> str:
    """Render ``value`` as a TOML basic string, quotes included.

    The template is a hand-commented starter file, so a whole-file TOML dump
    would throw the comments away; only the interpolated values are escaped.
    A basic string is used even where a literal ('...') one would read nicer
    for a command full of double quotes: literals cannot hold a single quote
    or a newline, so choosing between forms per value would need a fallback
    path anyway. One escaper that is always correct beats two that must agree.

    TOML requires escaping backslash, double quote and every control character
    (U+0000-U+001F and U+007F); the rest of Unicode passes through verbatim.
    """
    out = []
    for char in value:
        if char in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def find(start: Path | None = None) -> Path | None:
    """Search ``start`` and its ancestors for a ``vibe-caddy.toml``.

    Walking upward lets ``vibe-caddy start`` work from a subdirectory of the
    project, which is where a terminal usually sits.
    """
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def find_legacy(start: Path | None = None) -> Path | None:
    """Search for a pre-rename ``vibe.toml``, so the error can name it."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / LEGACY_CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load(path: Path) -> ProjectConfig:
    """Parse and validate a ``vibe.toml``.

    Raises:
        VibeError: if the file is malformed or has unknown or invalid keys.
    """
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VibeError(f"cannot read {path}: {exc}") from exc

    try:
        return ProjectConfig.model_validate(raw)
    except ValueError as exc:
        raise VibeError(f"{path} is not a valid vibe config:\n{exc}") from exc


def scaffold(
    directory: Path,
    name: str,
    cmd: str,
    icon: str = "zap",
    *,
    framework: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Write a commented starter ``vibe-caddy.toml``.

    Raises:
        VibeError: if one already exists and ``overwrite`` is not set; replacing
            it would silently discard the user's command and any pinned ports.
    """
    target = directory / CONFIG_NAME
    if target.exists() and not overwrite:
        raise VibeError(
            f"{target} already exists",
            hint="edit it, delete it, or pass --overwrite",
        )
    quoted_name = _toml_string(name)
    body = TEMPLATE.format(name=quoted_name, cmd=_toml_string(cmd), icon=_toml_string(icon))
    if framework:
        body = body.replace(
            f"name = {quoted_name}\n",
            f"name = {quoted_name}\n\n"
            "# Framework this was scaffolded from. Only picks the dashboard logo.\n"
            f"framework = {_toml_string(framework)}\n",
            1,
        )
    target.write_text(body)
    return target
