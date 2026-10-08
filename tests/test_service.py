"""Route lifecycle: registration, updates, state machine and ordering."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from vibe_caddy import launchd, paths, ports, registry, service, project
from vibe_caddy.errors import Conflict, NotFound, VibeError
from vibe_caddy.models import RegistryData, Route, RouteType


@pytest.fixture(autouse=True)
def quiet(reloads: list[object], monkeypatch: pytest.MonkeyPatch) -> None:
    """No Caddy reloads, and no dependence on which real ports happen to be bound."""
    monkeypatch.setattr(ports, "is_free", lambda port: True)
    monkeypatch.setattr(ports, "is_listening", lambda port: False)


def test_register_happy_path(reloads: list[object]) -> None:
    route = service.register("Web", port=3100, icon="W")
    assert (route.name, route.type, route.port, route.icon) == ("web", RouteType.STATIC, 3100, "W")
    assert registry.get("web") == route
    assert len(reloads) == 1


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"port": 3100}, RouteType.STATIC),
        ({"cmd": "x", "directory": Path("/tmp")}, RouteType.MANAGED),
        ({"url": "https://example.com"}, RouteType.BOOKMARK),
    ],
)
def test_register_infers_type(kwargs: dict[str, Any], expected: RouteType) -> None:
    assert service.register("web", **kwargs).type is expected


def test_register_sets_parent_for_worktree_names() -> None:
    assert service.register("feat.web", port=3100).parent == "web"


def test_register_duplicate_conflicts_unless_replaced() -> None:
    service.register("web", port=3100)
    with pytest.raises(Conflict, match="already exists"):
        service.register("web", port=3101)
    assert service.register("web", port=3101, replace=True).port == 3101


def test_register_rejects_invalid_name() -> None:
    with pytest.raises(ValueError, match="reserved"):
        service.register("localhost", port=3100)


def test_auto_reserved_port_differs_from_main_port() -> None:
    route = service.register("a", cmd="x", directory=Path("/tmp"), reserve_ports={"ws": None})
    assert route.port != route.reserve_ports["ws"]


def test_auto_port_avoids_claimed_ports() -> None:
    first = service.register("a", cmd="x", directory=Path("/tmp"))
    second = service.register("b", cmd="x", directory=Path("/tmp"))
    assert first.port == paths.PORT_RANGE[0]
    assert second.port == paths.PORT_RANGE[0] + 1


def test_resolve_port_rejects_port_held_by_another_route() -> None:
    service.register("a", port=3100)
    with pytest.raises(Conflict, match="'a'"):
        service.register("b", port=3100)


def test_resolve_port_rejects_managed_port_in_use_outside_vibe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vibe-caddy is about to bind a managed route's port itself, so it must be free."""
    monkeypatch.setattr(ports, "is_free", lambda port: False)
    monkeypatch.setattr(ports, "describe_holder", lambda port: "node")
    with pytest.raises(Conflict, match=r"in use \(held by node\)"):
        service.register("web", port=3100, cmd="x", directory=Path("/tmp"))
    assert registry.load().routes == {}


def test_resolve_port_own_port_is_not_a_conflict_on_replace() -> None:
    service.register("web", port=3100)
    assert service.register("web", port=3100, replace=True).port == 3100


def test_reserved_key_must_be_identifier() -> None:
    with pytest.raises(VibeError, match="not a valid"):
        service.register("web", cmd="x", directory=Path("/tmp"), reserve_ports={"bad-key": None})


def test_deregister_removes_route(reloads: list[object]) -> None:
    service.register("web", port=3100)
    assert service.deregister("web").name == "web"
    assert registry.load().routes == {}
    assert len(reloads) == 2


def test_deregister_missing_raises_not_found() -> None:
    with pytest.raises(NotFound, match="'nope'"):
        service.deregister("nope")


def test_deregister_managed_removes_plist() -> None:
    service.register("web", cmd="x", directory=Path("/tmp"))
    launchd.write_plist(registry.get("web"))
    service.deregister("web")
    assert not paths.app_plist("web").exists()


def test_update_patches_fields_and_reloads(reloads: list[object]) -> None:
    service.register("web", port=3100)
    updated = service.update("web", icon="Z", port=3200)
    assert (updated.icon, updated.port) == ("Z", 3200)
    assert registry.get("web").port == 3200
    assert len(reloads) == 2


def test_update_missing_raises_not_found() -> None:
    with pytest.raises(NotFound):
        service.update("nope", icon="x")


def test_update_port_collision() -> None:
    service.register("a", port=3100)
    service.register("b", port=3101)
    with pytest.raises(Conflict):
        service.update("b", port=3100)


def test_update_revalidates() -> None:
    service.register("bm", url="https://example.com")
    with pytest.raises(ValueError, match="bookmark routes require a url"):
        service.update("bm", url=None)
    # The failed patch must not have been persisted.
    assert str(registry.get("bm").url) == "https://example.com/"


def test_update_rebootstraps_loaded_managed_route(monkeypatch: pytest.MonkeyPatch) -> None:
    service.register("web", cmd="x", directory=Path("/tmp"))
    boots: list[Route] = []
    monkeypatch.setattr(launchd, "state", lambda label: launchd.JobState(True, pid=1))
    monkeypatch.setattr(launchd, "bootstrap", lambda route, shell=None: boots.append(route))
    service.update("web", cmd="y")
    assert [r.cmd for r in boots] == ["y"]


@pytest.mark.parametrize("name", ["start", "stop", "restart"])
def test_lifecycle_on_non_managed_route_raises(name: str) -> None:
    service.register("web", port=3100)
    with pytest.raises(VibeError, match="not a managed app"):
        getattr(service, name)("web")


def test_start_boots_a_managed_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service.register("web", cmd="x", directory=tmp_path)
    boots: list[str] = []
    monkeypatch.setattr(launchd, "bootstrap", lambda route, shell=None: boots.append(route.name))
    service.start("web")
    assert boots == ["web"]


def test_start_is_a_noop_when_already_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service.register("web", cmd="x", directory=tmp_path)
    monkeypatch.setattr(launchd, "state", lambda label: launchd.JobState(True, pid=7))
    monkeypatch.setattr(launchd, "bootstrap", lambda *a, **k: pytest.fail("should not boot"))
    assert service.start("web").name == "web"


def test_start_rejects_missing_directory_and_busy_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service.register("gone", cmd="x", directory=tmp_path / "missing")
    with pytest.raises(VibeError, match="is gone"):
        service.start("gone")

    service.register("busy", cmd="x", directory=tmp_path)
    monkeypatch.setattr(ports, "is_free", lambda port: False)
    with pytest.raises(Conflict, match="is in use"):
        service.start("busy")


def route_of(kind: RouteType, **extra: object) -> Route:
    fields: dict[str, object] = {"name": "r", "type": kind, "port": 3000}
    if kind in (RouteType.MANAGED, RouteType.WORKTREE):
        fields.update(cmd="x", dir="/tmp")
    if kind is RouteType.BOOKMARK:
        fields.update(port=None, url="https://example.com")
    return Route.model_validate(fields | extra)


@pytest.mark.parametrize(
    ("kind", "loaded", "pid", "listening", "state"),
    [
        (RouteType.MANAGED, True, 5, True, "ready"),
        (RouteType.WORKTREE, True, 5, True, "ready"),
        (RouteType.MANAGED, True, 5, False, "starting"),
        (RouteType.MANAGED, True, None, False, "crashed"),
        (RouteType.MANAGED, False, None, False, "stopped"),
        (RouteType.STATIC, False, None, True, "up"),
        (RouteType.STATIC, False, None, False, "down"),
    ],
)
def test_state_machine(
    kind: RouteType, loaded: bool, pid: int | None, listening: bool, state: str
) -> None:
    status = service.RouteStatus(route_of(kind), loaded, pid, listening)
    assert status.state == state


@pytest.mark.parametrize(
    ("job", "listening", "state"),
    [
        (launchd.JobState(True, pid=9), True, "ready"),
        (launchd.JobState(True, pid=9), False, "starting"),
        (launchd.JobState(True), False, "crashed"),
        (launchd.JobState(False), False, "stopped"),
    ],
)
def test_status_of_managed(
    monkeypatch: pytest.MonkeyPatch, job: launchd.JobState, listening: bool, state: str
) -> None:
    monkeypatch.setattr(launchd, "state", lambda label: job)
    monkeypatch.setattr(ports, "is_listening", lambda port: listening)
    assert service.status_of(route_of(RouteType.MANAGED)).state == state


def test_status_of_static_and_bookmark(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launchd, "state", lambda label: pytest.fail("static must not query"))
    assert service.status_of(route_of(RouteType.STATIC)).state == "down"
    monkeypatch.setattr(ports, "is_listening", lambda port: True)
    assert service.status_of(route_of(RouteType.STATIC)).state == "up"
    assert service.status_of(route_of(RouteType.BOOKMARK)).state == "up"


def test_statuses_sorts_worktrees_under_parent() -> None:
    def r(name: str, port: int, parent: str | None = None) -> Route:
        kind = RouteType.WORKTREE if parent else RouteType.MANAGED
        return Route(name=name, type=kind, port=port, cmd="x", dir="/tmp", parent=parent)

    rows = [
        r("zeta", 1),
        r("a.alpha", 2, "alpha"),
        r("alpha", 3),
        r("b.alpha", 4, "alpha"),
        r("beta", 5),
    ]
    registry.save(RegistryData(routes={x.name: x for x in rows}))
    assert [s.route.name for s in service.statuses()] == [
        "alpha",
        "a.alpha",
        "b.alpha",
        "beta",
        "zeta",
    ]


def test_prune_worktrees_removes_only_vanished_checkouts(tmp_path: Path) -> None:
    alive = tmp_path / "alive"
    (alive / ".git").mkdir(parents=True)
    service.register("alive.app", cmd="x", directory=alive, route_type=RouteType.WORKTREE)
    service.register(
        "gone.app", cmd="x", directory=tmp_path / "gone", route_type=RouteType.WORKTREE
    )
    assert service.prune_worktrees() == ["gone.app"]
    assert set(registry.load().routes) == {"alive.app"}


def test_register_replace_rebootstraps_a_loaded_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale plist would leave launchd on the old port while Caddy proxies the new one."""
    service.register("a", cmd="x", directory=Path("/tmp"), port=3100)
    monkeypatch.setattr(launchd, "state", lambda label: launchd.JobState(loaded=True, pid=1))
    bootstrapped: list[str] = []
    monkeypatch.setattr(
        launchd, "bootstrap", lambda route, shell=None: bootstrapped.append(route.name)
    )
    service.register("a", cmd="y", directory=Path("/tmp"), port=3101, replace=True)
    assert bootstrapped == ["a"]


@pytest.fixture
def loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let restart run its launchd steps without touching real launchd."""
    monkeypatch.setattr(launchd, "bootstrap", lambda route, shell=None: None)
    monkeypatch.setattr(launchd, "bootout", lambda label, quiet=False: True)


def test_restart_rereads_the_project_config(loaded: None, tmp_path: Path) -> None:
    """Editing the command and restarting is the obvious loop; it must work."""
    (tmp_path / project.CONFIG_NAME).write_text('name = "a"\ncmd = "first"\n')
    service.register("a", cmd="first", directory=tmp_path)
    (tmp_path / project.CONFIG_NAME).write_text('name = "a"\ncmd = "second"\n')
    assert service.restart("a").cmd == "second"


def test_restart_keeps_the_assigned_port(loaded: None, tmp_path: Path) -> None:
    (tmp_path / project.CONFIG_NAME).write_text('name = "a"\ncmd = "first"\n')
    original = service.register("a", cmd="first", directory=tmp_path)
    (tmp_path / project.CONFIG_NAME).write_text('name = "a"\ncmd = "second"\n')
    assert service.restart("a").port == original.port


def test_restart_survives_a_broken_project_config(loaded: None, tmp_path: Path) -> None:
    """A half-edited file must not make a running app unrestartable."""
    (tmp_path / project.CONFIG_NAME).write_text('name = "a"\ncmd = "good"\n')
    service.register("a", cmd="good", directory=tmp_path)
    (tmp_path / project.CONFIG_NAME).write_text("name = [broken")
    assert service.restart("a").cmd == "good"


def test_restart_works_without_any_project_file(loaded: None, tmp_path: Path) -> None:
    service.register("a", cmd="inline", directory=tmp_path)
    assert service.restart("a").cmd == "inline"


# ------------------------------------------------------- bulk deregistration


def test_deregister_many_reloads_caddy_once(reloads: list[object]) -> None:
    """Reloading per route would publish each intermediate state in turn."""
    for name in ("a", "b", "c"):
        service.register(name, port=None, cmd="x", directory=Path("/tmp"))
    before = len(reloads)
    service.deregister_many(["a", "b", "c"])
    assert len(reloads) - before == 1
    assert registry.load().routes == {}


def test_deregister_many_is_atomic_on_an_unknown_name() -> None:
    """A typo in a list must not half-apply."""
    service.register("a", port=3100)
    service.register("b", port=3101)
    with pytest.raises(NotFound):
        service.deregister_many(["a", "nope"])
    assert set(registry.load().routes) == {"a", "b"}


def test_deregister_many_tolerates_repeats() -> None:
    service.register("a", port=3100)
    assert [route.name for route in service.deregister_many(["a", "a"])] == ["a"]


def test_removable_excludes_the_dashboard_by_default() -> None:
    service.register(paths.DASHBOARD_ROUTE, cmd="x", directory=Path("/tmp"))
    service.register("app", port=3100)
    assert [route.name for route in service.removable()] == ["app"]
    assert paths.DASHBOARD_ROUTE in {r.name for r in service.removable(include_dashboard=True)}


def test_removable_orders_worktrees_before_their_parent() -> None:
    """A parent must not be dropped while a child route still points at it."""
    service.register("app", cmd="x", directory=Path("/tmp"))
    service.register("feat.app", cmd="x", directory=Path("/tmp"), route_type=RouteType.WORKTREE)
    assert [route.name for route in service.removable()] == ["feat.app", "app"]


def test_adding_a_command_promotes_a_static_route_to_managed(tmp_path: Path) -> None:
    """Otherwise the cmd is stored but the route never gains lifecycle controls."""
    service.register("a", port=3100)
    updated = service.update("a", cmd="npm run dev", dir=str(tmp_path))
    assert updated.type is RouteType.MANAGED
    assert updated.managed


def test_promotion_requires_a_working_directory() -> None:
    service.register("a", port=3100)
    with pytest.raises(VibeError, match="working directory"):
        service.update("a", cmd="npm run dev")


def test_promotion_keeps_the_assigned_port() -> None:
    original = service.register("a", port=3100)
    assert service.update("a", cmd="x", dir="/tmp").port == original.port


# ------------------------------------------------------------- review fixes


@pytest.fixture
def busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything is already listening: the port a user's own server holds."""
    monkeypatch.setattr(ports, "is_free", lambda port: False)
    monkeypatch.setattr(ports, "describe_holder", lambda port: "node")


@pytest.mark.usefixtures("busy")
def test_register_static_accepts_an_already_serving_port() -> None:
    """Finding 1: 'start my server, then give it a hostname' is the point of static."""
    assert service.register("mine", port=3100).port == 3100


@pytest.mark.usefixtures("busy")
def test_register_static_still_checks_the_registry() -> None:
    """Finding 1: accepting a listener must not accept another route's port."""
    service.register("a", port=3100)
    with pytest.raises(Conflict, match="'a'"):
        service.register("b", port=3100)


@pytest.mark.usefixtures("busy")
def test_update_accepts_the_unchanged_port_of_a_serving_route() -> None:
    """Finding 2: the dashboard's Save always submits the displayed port."""
    service.register("web", port=3100)
    assert service.update("web", port=3100, icon="Z").icon == "Z"


def test_update_still_validates_a_changed_managed_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service.register("web", cmd="x", directory=tmp_path, port=3100)
    monkeypatch.setattr(ports, "is_free", lambda port: False)
    with pytest.raises(Conflict, match="in use"):
        service.update("web", port=3200)


def test_update_refuses_main_port_equal_to_own_reserved_port(tmp_path: Path) -> None:
    """Finding 10: the route's own websocket port is not 'someone else's'."""
    service.register("web", cmd="x", directory=tmp_path, port=3100, reserve_ports={"ws": 3101})
    with pytest.raises(Conflict, match="another port of this route"):
        service.update("web", port=3101)
    assert registry.get("web").port == 3100


def test_update_auto_port_avoids_own_reserved_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finding 10: automatic reassignment must not hand back the reserved port."""
    service.register("web", cmd="x", directory=tmp_path, port=3105, reserve_ports={"ws": 3100})
    monkeypatch.setattr(
        ports,
        "find_free",
        lambda claimed=(): next(p for p in range(3100, 3200) if p not in claimed),
    )
    assert service.update("web", port=0).port == 3101


def test_managed_route_creation_requires_a_directory() -> None:
    """Finding 11: otherwise Start fails later in build_plist."""
    with pytest.raises(ValueError, match="require a dir"):
        Route(name="web", type=RouteType.MANAGED, port=3100, cmd="x")


def _project(tmp_path: Path, name: str, cmd: str = "x") -> Path:
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    (directory / project.CONFIG_NAME).write_text(f'name = "web"\ncmd = "{cmd}"\n')
    return directory


def test_start_project_refuses_a_different_project_with_the_same_name(
    loaded: None, tmp_path: Path
) -> None:
    """Finding 6: the second checkout must not take over the first one's route."""
    first = _project(tmp_path, "one", "first")
    second = _project(tmp_path, "two", "second")
    service.start_project(first)
    with pytest.raises(Conflict, match="already belongs") as caught:
        service.start_project(second)
    assert str(first) in str(caught.value)
    route = registry.get("web")
    assert (route.dir, route.cmd) == (str(first), "first")


def test_start_project_refreshes_the_same_project(loaded: None, tmp_path: Path) -> None:
    directory = _project(tmp_path, "one", "first")
    service.start_project(directory)
    _project(tmp_path, "one", "second")
    assert service.start_project(directory).cmd == "second"


def test_update_removes_autostart_symlink_for_a_stopped_route(tmp_path: Path) -> None:
    """Finding 7: a stopped job's login symlink must follow the saved setting."""
    service.register("web", cmd="x", directory=tmp_path, autostart=True)
    launchd.write_plist(registry.get("web"))
    link = paths.agent_symlink("web")
    assert link.is_symlink()
    service.update("web", autostart=False)
    assert not link.is_symlink()
    service.update("web", autostart=True)
    assert link.is_symlink()


def test_update_restarts_only_when_the_process_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Findings 7 and 13: an icon or autostart edit must not kill a running app."""
    service.register("web", cmd="x", directory=tmp_path)
    boots: list[str] = []
    monkeypatch.setattr(launchd, "state", lambda label: launchd.JobState(True, pid=1))
    monkeypatch.setattr(launchd, "bootstrap", lambda route, shell=None: boots.append(route.cmd))
    service.update("web", icon="Z", autostart=True, framework="next")
    assert boots == []
    service.update("web", cmd="y")
    assert boots == ["y"]


def test_restart_bootstraps_exactly_once_when_the_project_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finding 13: refreshing config and restarting were two lifecycle transitions."""
    (tmp_path / project.CONFIG_NAME).write_text('name = "a"\ncmd = "first"\n')
    service.register("a", cmd="first", directory=tmp_path)
    (tmp_path / project.CONFIG_NAME).write_text('name = "a"\ncmd = "second"\n')
    boots: list[str | None] = []
    monkeypatch.setattr(launchd, "state", lambda label: launchd.JobState(True, pid=1))
    monkeypatch.setattr(launchd, "bootstrap", lambda route, shell=None: boots.append(route.cmd))
    service.restart("a")
    assert boots == ["second"]


def _launchctl(monkeypatch: pytest.MonkeyPatch, code: int, stderr: str) -> None:
    def run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, code, "", stderr)

    monkeypatch.setattr(launchd, "_run", run)


def test_stop_raises_when_launchctl_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finding 12: the CLI printed 'stopped' after 'Operation not permitted'."""
    service.register("web", cmd="x", directory=tmp_path)
    _launchctl(monkeypatch, 1, "Boot-out failed: 1: Operation not permitted")
    with pytest.raises(VibeError, match="Operation not permitted"):
        service.stop("web")


def test_stop_is_idempotent_for_an_absent_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service.register("web", cmd="x", directory=tmp_path)
    _launchctl(monkeypatch, 3, "Boot-out failed: 3: No such process")
    assert service.stop("web").name == "web"


def test_deregister_keeps_state_when_the_process_cannot_be_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finding 12: forgetting a job launchd still runs orphans its process."""
    service.register("web", cmd="x", directory=tmp_path)
    launchd.write_plist(registry.get("web"))
    monkeypatch.setattr(launchd, "state", lambda label: launchd.JobState(True, pid=1))
    _launchctl(monkeypatch, 1, "Boot-out failed: 1: Operation not permitted")
    with pytest.raises(VibeError, match="could not stop"):
        service.deregister("web")
    assert "web" in registry.load().routes
    assert paths.app_plist("web").exists()


def test_start_waits_for_a_port_the_previous_process_is_still_releasing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`stop` then `start` must not report the route's own dying process as a conflict."""
    service.register("a", port=3100, cmd="x", directory=tmp_path)
    monkeypatch.setattr(launchd, "state", lambda label: launchd.JobState(loaded=False))
    started: list[str] = []
    monkeypatch.setattr(launchd, "bootstrap", lambda route, shell=None: started.append(route.name))

    # Busy for the first two probes, then released, as a real shutdown behaves.
    probes = iter([False, False, True])
    monkeypatch.setattr(ports, "is_free", lambda port: next(probes, True))
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)

    service.start("a")
    assert started == ["a"]


def test_start_still_reports_a_port_held_by_something_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service.register("a", port=3100, cmd="x", directory=tmp_path)
    monkeypatch.setattr(launchd, "state", lambda label: launchd.JobState(loaded=False))
    monkeypatch.setattr(ports, "is_free", lambda port: False)
    monkeypatch.setattr(ports, "describe_holder", lambda port: "nginx")
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)
    with pytest.raises(Conflict, match="nginx"):
        service.start("a")
