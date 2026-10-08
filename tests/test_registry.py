"""Registry persistence, locking semantics and port-claim bookkeeping."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from vibe_caddy import paths, registry
from vibe_caddy.errors import Conflict, NotFound
from vibe_caddy.models import RegistryData, Route, RouteType


def sample() -> RegistryData:
    return RegistryData(
        routes={
            "web": Route(
                name="web",
                type=RouteType.MANAGED,
                port=3000,
                cmd="npm run dev",
                dir="/tmp/web",
                reserve_ports={"ws": 3001},
                created_at=dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
            ),
            "bm": Route(
                name="bm",
                type=RouteType.BOOKMARK,
                url="https://example.com/path",
                proxy=True,
                insecure_skip_verify=True,
            ),
        }
    )


def test_load_missing_file_is_empty() -> None:
    assert registry.load() == RegistryData()
    assert not paths.registry_file().exists()


def test_round_trip_preserves_every_field_type() -> None:
    original = sample()
    registry.save(original)
    loaded = registry.load()
    assert loaded == original
    web, bm = loaded.routes["web"], loaded.routes["bm"]
    assert web.type is RouteType.MANAGED
    assert web.created_at == dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    assert web.reserve_ports == {"ws": 3001}
    assert str(bm.url) == "https://example.com/path"


def test_save_leaves_no_temp_files() -> None:
    registry.save(sample())
    registry.save(sample())
    assert sorted(p.name for p in paths.registry_file().parent.iterdir() if p.is_file()) == [
        "registry.json"
    ]


def test_save_failure_keeps_old_file_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    registry.save(sample())
    before = paths.registry_file().read_text()

    def boom(self: object, target: object) -> None:
        raise OSError("disk full")

    # A nested context so only this patch is undone, not the autouse home isolation.
    with monkeypatch.context() as inner:
        inner.setattr("pathlib.Path.replace", boom)
        with pytest.raises(OSError, match="disk full"):
            registry.save(RegistryData())

    assert paths.registry_file().read_text() == before
    assert [p.name for p in paths.registry_file().parent.glob(".registry-*")] == []


@pytest.mark.parametrize("content", ["{not json", '{"version": 2}', '{"routes": {"x": {}}}'])
def test_corrupt_registry_raises_conflict(content: str) -> None:
    paths.ensure_dirs()
    paths.registry_file().write_text(content)
    with pytest.raises(Conflict, match="not valid"):
        registry.load()


def test_transaction_persists_on_clean_exit() -> None:
    with registry.transaction() as data:
        data.routes["web"] = Route(name="web", type=RouteType.STATIC, port=3000)
    assert "web" in registry.load().routes
    assert json.loads(paths.registry_file().read_text())["routes"]["web"]["port"] == 3000


def test_transaction_does_not_persist_when_body_raises() -> None:
    with pytest.raises(RuntimeError), registry.transaction() as data:
        data.routes["web"] = Route(name="web", type=RouteType.STATIC, port=3000)
        raise RuntimeError("abort")
    assert registry.load().routes == {}


def test_get_unknown_names_registered_routes() -> None:
    registry.save(sample())
    with pytest.raises(NotFound, match="'nope'") as info:
        registry.get("nope")
    assert "bm, web" in (info.value.hint or "")
    assert registry.get("web").port == 3000


@pytest.mark.parametrize(
    ("excluding", "expected"),
    [(None, {3000, 3001}), ("web", set()), ("bm", {3000, 3001}), ("other", {3000, 3001})],
)
def test_claimed_ports(excluding: str | None, expected: set[int]) -> None:
    assert registry.claimed_ports(sample(), excluding=excluding) == expected


@pytest.mark.parametrize("port", [3000, 3001])
def test_assert_port_available_names_holder(port: int) -> None:
    with pytest.raises(Conflict, match="'web'"):
        registry.assert_port_available(sample(), port)


def test_assert_port_available_passes_for_free_or_excluded() -> None:
    registry.assert_port_available(sample(), 3999)
    registry.assert_port_available(sample(), 3000, excluding="web")
