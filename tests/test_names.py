"""Route-name validation, parent lookup and slugification."""

from __future__ import annotations

import pytest

from vibe_caddy import names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("web", "web"),
        ("my-app", "my-app"),
        ("a", "a"),
        ("app2", "app2"),
        ("Web", "web"),
        ("  Padded  ", "padded"),
        ("feat.web", "feat.web"),
        ("a" * 63, "a" * 63),
    ],
)
def test_validate_accepts(raw: str, expected: str) -> None:
    assert names.validate(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "a.b.c",
        "-web",
        "web-",
        "we b",
        "we_b",
        "a..b",
        ".web",
        "web.",
        "a" * 64,
        "feat." + "a" * 64,
        "local",
        "localhost",
        "LOCALHOST",
    ],
)
def test_validate_rejects(raw: str) -> None:
    with pytest.raises(names.InvalidName):
        names.validate(raw)


@pytest.mark.parametrize(
    ("name", "parent"), [("web", None), ("feat.web", "web"), ("a.b", "b"), ("x.", None)]
)
def test_parent_of(name: str, parent: str | None) -> None:
    assert names.parent_of(name) == parent


@pytest.mark.parametrize(
    ("value", "slug"),
    [
        ("main", "main"),
        ("Feature/Login Page", "feature-login-page"),
        ("worktree-fix-bug", "fix-bug"),
        ("Worktree-Fix", "fix"),
        ("--weird__name--", "weird-name"),
        ("café déjà vu", "caf-d-j-vu"),
        ("a!!!b???c", "a-b-c"),
        ("worktree-", "worktree"),
        ("!!!", ""),
        ("x" * 100, "x" * 63),
    ],
)
def test_slugify(value: str, slug: str) -> None:
    assert names.slugify(value) == slug


def test_slugify_truncation_never_ends_in_hyphen() -> None:
    assert not names.slugify("a" * 62 + "-bbb").endswith("-")
