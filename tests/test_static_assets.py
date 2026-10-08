"""The dashboard's static assets must parse.

A syntax error in app.js is invisible to every other test here: the server still
serves the file with a 200, the HTML still renders, and only a real browser ever
discovers that the whole script failed to parse and no control works. These
tests are the cheapest stand-in for loading the page.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src/vibe_caddy/dashboard/static"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to parse JavaScript")
def test_app_js_parses() -> None:
    result = subprocess.run(
        ["node", "--check", str(STATIC / "app.js")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_stylesheets_have_balanced_braces() -> None:
    """A stray brace silently drops every rule after it."""
    for sheet in STATIC.glob("*.css"):
        text = re.sub(r"/\*.*?\*/", "", sheet.read_text(), flags=re.DOTALL)
        assert text.count("{") == text.count("}"), f"{sheet.name} has unbalanced braces"


def test_every_vendored_icon_has_a_mask_rule() -> None:
    """A logo with no rule renders as an empty box rather than falling back."""
    rules = set(
        re.findall(r'\.logo\[data-framework="([^"]+)"\]', (STATIC / "icons.css").read_text())
    )
    files = {path.stem for path in (STATIC / "icons").glob("*.svg")}
    assert files == rules, f"icons.css and icons/ disagree: {files ^ rules}"


def test_vendored_icons_are_monochrome() -> None:
    """A coloured icon would ignore the CSS mask tint and render as a solid block."""
    for icon in (STATIC / "icons").glob("*.svg"):
        text = icon.read_text()
        assert "fill=" not in text, f"{icon.name} carries a fill"
        assert "#" not in text, f"{icon.name} carries a hard-coded colour"


def test_no_asset_references_an_external_origin() -> None:
    """The dashboard must render identically offline."""
    for asset in [*STATIC.glob("*.css"), *STATIC.glob("*.js")]:
        text = asset.read_text()
        assert "http://" not in text, f"{asset.name} references an external origin"
        assert "https://" not in text, f"{asset.name} references an external origin"
