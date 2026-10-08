"""Caddyfile generation, checked structurally and against the real ``caddy`` binary."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from vibe_caddy import caddy, paths, registry
from vibe_caddy.errors import VibeError
from vibe_caddy.models import RegistryData, Route, RouteType

needs_caddy = pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy not installed")


def full_registry() -> RegistryData:
    routes = [
        Route(name="app", type=RouteType.MANAGED, port=3000, cmd="x", dir="/tmp/app"),
        Route(name="feat.app", type=RouteType.WORKTREE, port=3001, cmd="x", dir="/tmp/wt"),
        Route(name="static", type=RouteType.STATIC, port=3002, ws_origin_rewrite=False),
        Route(name="redir", type=RouteType.BOOKMARK, url="https://example.com/docs/"),
        Route(
            name="proxied",
            type=RouteType.BOOKMARK,
            url="https://192.168.1.10:8443",
            proxy=True,
            insecure_skip_verify=True,
        ),
    ]
    return RegistryData(routes={r.name: r for r in routes})


def site_blocks(text: str) -> list[str]:
    """Split the per-route site blocks out of a rendered Caddyfile."""
    return [
        b for b in text.split("\n}\n") if b.lstrip().startswith("https://") and "localhost" in b
    ]


@pytest.fixture
def caddyfile_path(tmp_path: Path) -> Path:
    return tmp_path / "Caddyfile"


def run_caddy(*args: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "xdg"), "XDG_CONFIG_HOME": str(tmp_path)}
    return subprocess.run(["caddy", *args], capture_output=True, text=True, env=env, timeout=60)


@needs_caddy
@pytest.mark.parametrize(
    "data",
    [full_registry(), RegistryData()],
    ids=["full", "empty"],
)
@pytest.mark.parametrize("dashboard_port", [None, 7999], ids=["no-dashboard", "dashboard"])
def test_render_is_valid_and_canonical(
    data: RegistryData, dashboard_port: int | None, caddyfile_path: Path, tmp_path: Path
) -> None:
    caddyfile_path.write_text(caddy.render(data, dashboard_port))

    validate = run_caddy(
        "validate", "--adapter", "caddyfile", "--config", str(caddyfile_path), tmp_path=tmp_path
    )
    assert validate.returncode == 0, validate.stderr

    fmt = run_caddy("fmt", "--diff", str(caddyfile_path), tmp_path=tmp_path)
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr
    # With no formatting diff, caddy echoes the file as unchanged context lines only.
    assert not [ln for ln in fmt.stdout.splitlines() if ln[:1] in "+-"]


def test_every_route_hostname_appears() -> None:
    text = caddy.render(full_registry())
    for route in full_registry().routes.values():
        assert f"https://{route.hostname}, http://{route.hostname} {{" in text


def test_ws_matcher_only_when_origin_rewrite_enabled() -> None:
    blocks = {
        b.split("https://")[1].split(",")[0]: b for b in site_blocks(caddy.render(full_registry()))
    }
    assert "@upgrade" in blocks["app.localhost"]
    assert "@upgrade" in blocks["feat.app.localhost"]
    assert "@upgrade" not in blocks["static.localhost"]
    assert "header_up Origin http://127.0.0.1:3000" in blocks["app.localhost"]
    assert "reverse_proxy 127.0.0.1:3002" in blocks["static.localhost"]


def test_bind_in_every_site_block_including_catch_all() -> None:
    text = caddy.render(full_registry())
    blocks = site_blocks(text)
    assert len(blocks) == 5
    assert all("bind 127.0.0.1 ::1" in b for b in blocks)
    catch_all = text[text.index("https://, http:// {") :]
    assert "bind 127.0.0.1 ::1" in catch_all
    assert "on_demand" in catch_all


def test_bookmark_rendering() -> None:
    text = caddy.render(full_registry())
    assert "redir https://example.com/docs{uri} 307" in text
    assert "reverse_proxy https://192.168.1.10:8443 {" in text
    assert text.count("tls_insecure_skip_verify") == 1


@pytest.mark.parametrize(
    ("dashboard_port", "expected", "absent"),
    [
        (None, "respond ", "reverse_proxy 127.0.0.1:7999"),
        (7999, "reverse_proxy 127.0.0.1:7999", ""),
    ],
)
def test_catch_all_dashboard(dashboard_port: int | None, expected: str, absent: str) -> None:
    catch_all = caddy.render(RegistryData(), dashboard_port).split("https://, http:// {")[1]
    assert expected in catch_all
    if absent:
        assert absent not in catch_all
    else:
        assert "respond" not in catch_all.split("on_demand")[1]


def test_no_dashboard_catch_all_has_no_reverse_proxy() -> None:
    catch_all = caddy.render(RegistryData(), None).split("https://, http:// {")[1]
    assert "reverse_proxy" not in catch_all
    assert "404" in catch_all


def test_routes_are_sorted_by_name() -> None:
    text = caddy.render(full_registry())
    hosts = [h for h in (f"{n}.localhost" for n in sorted(full_registry().routes))]
    positions = [text.index(f"https://{h},") for h in hosts]
    assert positions == sorted(positions)


def test_write_creates_file_under_state_dir() -> None:
    content = caddy.write(RegistryData(), None)
    assert paths.caddyfile().read_text() == content


@pytest.mark.parametrize(
    ("routes", "port"),
    [({}, None), ({"vibe": Route(name="vibe", type=RouteType.STATIC, port=4000)}, 4000)],
)
def test_dashboard_port_of(routes: dict[str, Route], port: int | None) -> None:
    assert caddy.dashboard_port_of(RegistryData(routes=routes)) == port


def proxied(name: str, url: str) -> Route:
    return Route(name=name, type=RouteType.BOOKMARK, url=url, proxy=True)


def bookmark_registry() -> RegistryData:
    routes = [
        proxied("pathed", "https://example.com/docs/"),
        proxied("queried", "https://example.com/docs?lang=en"),
        proxied("query-only", "https://example.com/?lang=en"),
        proxied("root", "https://example.com/"),
        proxied("bare", "https://example.com"),
        proxied("ported", "https://192.168.1.10:8443/ui/"),
        Route(name="redir", type=RouteType.BOOKMARK, url="https://example.com/docs/?a=1"),
    ]
    return RegistryData(routes={r.name: r for r in routes})


def block_for(text: str, name: str) -> str:
    prefix = f"https://{name}.localhost,"
    return next(b.lstrip() for b in site_blocks(text) if b.lstrip().startswith(prefix))


@needs_caddy
def test_proxied_bookmarks_with_paths_are_valid_and_canonical(
    caddyfile_path: Path, tmp_path: Path
) -> None:
    caddyfile_path.write_text(caddy.render(bookmark_registry(), None))

    validate = run_caddy(
        "validate", "--adapter", "caddyfile", "--config", str(caddyfile_path), tmp_path=tmp_path
    )
    assert validate.returncode == 0, validate.stderr
    fmt = run_caddy("fmt", "--diff", str(caddyfile_path), tmp_path=tmp_path)
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr
    assert not [ln for ln in fmt.stdout.splitlines() if ln[:1] in "+-"]


def test_proxied_bookmark_path_becomes_rewrite_not_upstream() -> None:
    block = block_for(caddy.render(bookmark_registry()), "pathed")
    assert "reverse_proxy https://example.com {" in block
    assert "rewrite * /docs{path}\n" in block
    assert "/docs/ " not in block.split("reverse_proxy")[1]


def test_proxied_bookmark_query_is_carried_in_rewrite() -> None:
    text = caddy.render(bookmark_registry())
    assert "rewrite * /docs{path}?lang=en&{query}" in block_for(text, "queried")
    query_only = block_for(text, "query-only")
    assert "rewrite * {path}?lang=en&{query}" in query_only
    assert "reverse_proxy https://example.com {" in query_only


@pytest.mark.parametrize("name", ["root", "bare"])
def test_proxied_bookmark_at_root_needs_no_rewrite(name: str) -> None:
    block = block_for(caddy.render(bookmark_registry()), name)
    assert "rewrite" not in block
    assert "reverse_proxy https://example.com {" in block


def test_proxied_bookmark_keeps_explicit_port() -> None:
    block = block_for(caddy.render(bookmark_registry()), "ported")
    assert "reverse_proxy https://192.168.1.10:8443 {" in block
    assert "rewrite * /ui{path}" in block


def test_redirect_bookmark_keeps_full_url() -> None:
    block = block_for(caddy.render(bookmark_registry()), "redir")
    assert "redir https://example.com/docs/?a=1{uri} 307" in block
    assert "rewrite" not in block


@needs_caddy
def test_invalid_candidate_never_replaces_known_good_caddyfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caddy.write(RegistryData(), None)
    good = paths.caddyfile().read_text()
    # Force an invalid render to stand in for any future generator bug.
    monkeypatch.setattr(caddy, "render", lambda *_a, **_k: "this is { not caddy\n")

    with pytest.raises(VibeError, match="invalid") as excinfo:
        caddy.write(RegistryData(), None)
    assert "no detail given" not in str(excinfo.value)
    with pytest.raises(VibeError):
        caddy.reload(require_running=False)

    assert paths.caddyfile().read_text() == good
    assert [p.name for p in paths.state_dir().glob(".Caddyfile-*")] == []


def test_concurrent_reloads_publish_the_latest_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commits, stalls mid-publication; B commits and reloads; A must not win last."""
    monkeypatch.setattr(caddy, "is_running", lambda timeout=2.0: False)
    monkeypatch.setattr(caddy, "validate", lambda path=None: None)

    real_render = caddy.render
    a_rendered = threading.Event()
    release_a = threading.Event()
    calls = []

    def gated_render(data: RegistryData, dashboard_port: int | None = None) -> str:
        content = real_render(data, dashboard_port)
        calls.append(threading.current_thread().name)
        if threading.current_thread().name == "A":
            a_rendered.set()
            assert release_a.wait(timeout=10)
        return content

    monkeypatch.setattr(caddy, "render", gated_render)

    def register(name: str, port: int) -> None:
        with registry.transaction() as data:
            data.routes[name] = Route(name=name, type=RouteType.STATIC, port=port)
        caddy.reload(require_running=False)

    thread_a = threading.Thread(target=register, args=("a", 4001), name="A")
    thread_b = threading.Thread(target=register, args=("b", 4002), name="B")
    thread_a.start()
    assert a_rendered.wait(timeout=10)
    thread_b.start()
    # Without publication serialization B finishes here, and A then overwrites it
    # with its stale render. With it B is blocked on A, so give it a bounded
    # chance to finish and release A either way.
    thread_b.join(timeout=0.3)
    release_a.set()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    text = paths.caddyfile().read_text()
    assert "a.localhost" in text
    assert "b.localhost" in text
