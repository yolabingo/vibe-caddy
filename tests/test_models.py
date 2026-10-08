"""Route model validation and derived properties."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibe_caddy.models import Route, RouteType


def make(**overrides: object) -> Route:
    base: dict[str, object] = {"name": "web", "type": RouteType.STATIC, "port": 3000}
    base.update(overrides)
    return Route.model_validate(base)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"type": RouteType.BOOKMARK, "port": None}, "bookmark routes require a url"),
        ({"type": RouteType.STATIC, "port": None}, "require a port"),
        ({"type": RouteType.MANAGED, "port": None, "cmd": "x"}, "require a port"),
        ({"type": RouteType.MANAGED}, "require a cmd"),
        ({"type": RouteType.WORKTREE}, "require a cmd"),
        ({"type": RouteType.MANAGED, "cmd": "x"}, "require a dir"),
        ({"type": RouteType.WORKTREE, "cmd": "x"}, "require a dir"),
        ({"proxy": True}, "proxy requires a url"),
        ({"port": 0}, "greater than or equal to 1"),
        ({"port": 70000}, "less than or equal to 65535"),
        ({"bogus": 1}, "Extra inputs"),
    ],
)
def test_invalid_shapes(fields: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make(**fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"type": RouteType.STATIC},
        {"type": RouteType.MANAGED, "cmd": "npm run dev", "dir": "/tmp"},
        {"type": RouteType.WORKTREE, "cmd": "npm run dev", "dir": "/tmp"},
        {"type": RouteType.BOOKMARK, "port": None, "url": "https://example.com"},
        {"type": RouteType.BOOKMARK, "port": None, "url": "https://example.com", "proxy": True},
    ],
)
def test_valid_shapes(fields: dict[str, object]) -> None:
    assert make(**fields).name == "web"


def test_all_ports_merges_port_and_reserved() -> None:
    route = make(reserve_ports={"ws": 3001, "db": 3002})
    assert route.all_ports() == {3000, 3001, 3002}


def test_all_ports_of_bookmark_is_empty() -> None:
    route = make(type=RouteType.BOOKMARK, port=None, url="https://example.com")
    assert route.all_ports() == set()


@pytest.mark.parametrize(
    ("fields", "href"),
    [
        (
            {"type": RouteType.BOOKMARK, "port": None, "url": "https://example.com/x"},
            "https://example.com/x",
        ),
        (
            {"type": RouteType.BOOKMARK, "port": None, "url": "https://example.com", "proxy": True},
            "https://web.localhost",
        ),
        ({}, "https://web.localhost"),
    ],
)
def test_href(fields: dict[str, object], href: str) -> None:
    assert make(**fields).href == href


@pytest.mark.parametrize(
    ("route_type", "managed"),
    [
        (RouteType.MANAGED, True),
        (RouteType.WORKTREE, True),
        (RouteType.STATIC, False),
        (RouteType.BOOKMARK, False),
    ],
)
def test_managed_property(route_type: RouteType, managed: bool) -> None:
    fields: dict[str, object] = {"type": route_type, "cmd": "x", "dir": "/tmp"}
    if route_type is RouteType.BOOKMARK:
        fields.update(port=None, url="https://example.com")
    assert make(**fields).managed is managed


def test_hostname() -> None:
    assert make().hostname == "web.localhost"
