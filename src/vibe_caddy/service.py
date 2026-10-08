"""Route lifecycle: the operations the CLI and the dashboard both perform.

Every mutation follows the same shape -- take the registry lock, change the
registry, then regenerate the Caddyfile and reload Caddy -- so that the proxy and
the registry can never disagree about which names exist.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import caddy, frameworks, gitwt, launchd, names, paths, ports, progress, project, registry
from .errors import Conflict, NotFound, VibeError
from .models import ProjectConfig, RegistryData, Route, RouteType


@dataclass(frozen=True, slots=True)
class RouteStatus:
    """A route joined with what launchd and the network currently report."""

    route: Route
    loaded: bool
    pid: int | None
    listening: bool

    @property
    def state(self) -> str:
        """One word for a table cell."""
        if not self.route.managed:
            return "up" if self.listening else "down"
        if self.listening:
            return "ready"
        if self.pid is not None:
            return "starting"
        if self.loaded:
            return "crashed"
        return "stopped"


def status_of(route: Route) -> RouteStatus:
    """Collect the live state of one route."""
    if route.type is RouteType.BOOKMARK:
        return RouteStatus(route=route, loaded=False, pid=None, listening=True)

    job = launchd.state(paths.app_label(route.name)) if route.managed else launchd.JobState(False)
    listening = route.port is not None and ports.is_listening(route.port)
    return RouteStatus(route=route, loaded=job.loaded, pid=job.pid, listening=listening)


def statuses() -> list[RouteStatus]:
    """Live state for every registered route, ordered for display.

    Worktree routes sort directly under the app they belong to.
    """
    data = registry.load()

    def sort_key(route: Route) -> tuple[str, int, str]:
        return (route.parent or route.name, 1 if route.parent else 0, route.name)

    return [status_of(route) for route in sorted(data.routes.values(), key=sort_key)]


# --------------------------------------------------------------------- register


def _resolve_port(
    data: RegistryData,
    requested: int | None,
    *,
    excluding: str | None,
    require_free: bool = True,
    also_claimed: Iterable[int] = (),
) -> int:
    """Validate a requested port, or assign one.

    Args:
        require_free: Refuse a port something is already listening on. Only a route
            vibe-caddy is about to bind itself needs this; a static route exists to
            name a server the user already started, so an occupied port is its
            normal case.
        also_claimed: Ports the same route already holds that are not in the
            registry under ``excluding`` -- its own auxiliary ports, which the
            exclusion would otherwise hide from the collision check.

    Raises:
        Conflict: if the requested port belongs to another route, collides with
            one of this route's own ports, or (when ``require_free``) is already in
            use by something outside vibe.
    """
    own = set(also_claimed)
    if requested is None or requested == 0:
        return ports.find_free(registry.claimed_ports(data, excluding=excluding) | own)

    registry.assert_port_available(data, requested, excluding=excluding)
    if requested in own:
        raise Conflict(
            f"port {requested} is already used by another port of this route",
            hint="pick another port, or omit it to have one assigned",
        )
    if require_free and not ports.is_free(requested):
        holder = ports.describe_holder(requested)
        detail = f" (held by {holder})" if holder else ""
        raise Conflict(
            f"port {requested} is already in use{detail}",
            hint="stop that process, or omit the port to have one assigned",
        )
    return requested


def _resolve_reserved(
    data: RegistryData,
    requested: dict[str, int | None],
    *,
    excluding: str | None,
    also_claimed: Iterable[int] = (),
) -> dict[str, int]:
    """Assign any unpinned auxiliary ports, validating the pinned ones.

    Args:
        also_claimed: Ports already handed to this same route -- in practice its
            own main port, which is not in the registry yet and would otherwise be
            handed out a second time as an auxiliary port.
    """
    resolved: dict[str, int] = {}
    for key, value in requested.items():
        if not key.isidentifier():
            raise VibeError(f"reserve_ports key {key!r} is not a valid environment-variable name")
        claimed = (
            registry.claimed_ports(data, excluding=excluding)
            | set(resolved.values())
            | set(also_claimed)
        )
        if value in (None, 0):
            resolved[key] = ports.find_free(claimed)
            continue
        assert value is not None  # narrowed by the branch above
        if value in claimed:
            raise Conflict(
                f"reserve_ports.{key} = {value} collides with another port this route uses"
            )
        resolved[key] = _resolve_port(data, value, excluding=excluding)
    return resolved


def register(
    name: str,
    *,
    port: int | None = None,
    cmd: str | None = None,
    directory: Path | None = None,
    url: str | None = None,
    proxy: bool = False,
    insecure_skip_verify: bool = False,
    icon: str | None = None,
    framework: str | None = None,
    autostart: bool = False,
    ws_origin_rewrite: bool = True,
    reserve_ports: dict[str, int | None] | None = None,
    route_type: RouteType | None = None,
    parent: str | None = None,
    replace: bool = False,
) -> Route:
    """Create a route and reload Caddy.

    Raises:
        Conflict: if the name is taken and ``replace`` is not set, or a port
            collides.
    """
    canonical = names.validate(name)

    if route_type is None:
        route_type = RouteType.BOOKMARK if url else (RouteType.MANAGED if cmd else RouteType.STATIC)

    with registry.transaction() as data:
        if canonical in data.routes and not replace:
            raise Conflict(
                f"route {canonical!r} already exists",
                hint=f"remove it first: vibe-caddy deregister {canonical}",
            )

        resolved_port: int | None = None
        resolved_reserved: dict[str, int] = {}
        if route_type is not RouteType.BOOKMARK:
            resolved_port = _resolve_port(
                data,
                port,
                excluding=canonical,
                require_free=route_type in (RouteType.MANAGED, RouteType.WORKTREE),
            )
            resolved_reserved = _resolve_reserved(
                data,
                reserve_ports or {},
                excluding=canonical,
                also_claimed=[resolved_port],
            )

        route = Route(
            name=canonical,
            type=route_type,
            port=resolved_port,
            cmd=cmd,
            dir=str(directory) if directory else None,
            parent=parent or names.parent_of(canonical),
            url=url,  # pyright: ignore[reportArgumentType]
            proxy=proxy,
            insecure_skip_verify=insecure_skip_verify,
            icon=icon,
            framework=framework,
            autostart=autostart,
            ws_origin_rewrite=ws_origin_rewrite,
            reserve_ports=resolved_reserved,
        )
        data.routes[canonical] = route

    # Replacing a route whose job is already loaded leaves launchd running the
    # previous plist -- an old port, command or shell -- while the registry and
    # the Caddyfile describe the new one. Re-bootstrap so the three agree.
    if route.managed and launchd.state(paths.app_label(canonical)).loaded:
        launchd.bootstrap(route)

    caddy.reload(require_running=False)
    return route


def deregister(name: str) -> Route:
    """Stop the route if it is managed, drop it, and reload Caddy."""
    return deregister_many([name])[0]


def deregister_many(names: Sequence[str]) -> list[Route]:
    """Remove several routes in one go.

    The registry is edited once and Caddy is reloaded once, rather than per
    route: reloading in a loop would leave the proxy briefly serving each
    intermediate state, and would be N times the work for no benefit.

    Raises:
        NotFound: if any name is unknown. Nothing is removed in that case, so a
            typo in a list cannot half-apply.
    """
    requested = list(dict.fromkeys(names))

    with registry.transaction() as data:
        missing = [name for name in requested if name not in data.routes]
        if missing:
            known = ", ".join(sorted(data.routes)) or "none"
            raise NotFound(
                f"no route named {missing[0]!r}"
                if len(missing) == 1
                else f"unknown routes: {', '.join(missing)}",
                hint=f"registered: {known}",
            )
        removed: list[Route] = []
        failures: list[str] = []
        for name in requested:
            route = data.routes[name]
            # Stop before forgetting: dropping the registry entry and plist of a
            # job launchd still runs would orphan a process nothing can stop and
            # free its port for reuse while it is still bound.
            if route.managed:
                label = paths.app_label(name)
                try:
                    if launchd.state(label).loaded:
                        launchd.bootout(label)
                except VibeError as exc:
                    failures.append(f"{name}: {exc}")
                    continue
                launchd.remove_plist(name)
            removed.append(data.routes.pop(name))

    caddy.reload(require_running=False)
    if failures:
        raise VibeError(
            "could not stop: " + "; ".join(failures),
            hint="those routes are still registered; stop them, then deregister again",
        )
    return removed


def removable(*, include_dashboard: bool = False) -> list[Route]:
    """Every route ``--all`` would remove, in the order it would remove them.

    Worktree routes come before their parent so a parent is never dropped while
    a child still points at it. vibe-caddy's own dashboard is left alone unless
    asked for: it is infrastructure the user did not register.
    """
    routes = [
        route
        for route in registry.load().routes.values()
        if include_dashboard or route.name != paths.DASHBOARD_ROUTE
    ]
    return sorted(routes, key=lambda route: (route.parent is None, route.name))


def _runtime_key(route: Route) -> tuple[object, ...]:
    """The fields that make up the running process, as launchd was given them.

    Anything not listed here -- icon, framework, autostart, the WebSocket origin
    rewrite -- is read by Caddy or the dashboard, so changing it must not kill a
    process the user is in the middle of using.
    """
    return (route.name, route.cmd, route.dir, route.port, sorted(route.reserve_ports.items()))


def _update(name: str, fields: dict[str, Any]) -> tuple[Route, Route]:
    """Store a patch, reconcile launchd's files and reload Caddy, without touching the process.

    Returns:
        The route as it was, and as it is now.
    """
    with registry.transaction() as data:
        route = data.routes.get(name)
        if route is None:
            raise NotFound(f"no route named {name!r}")

        # Giving a static route a command is how it becomes an app vibe-caddy
        # runs; without this the cmd would be stored but the route would stay
        # unmanaged, showing no lifecycle controls and never starting.
        if fields.get("cmd") and route.type is RouteType.STATIC:
            directory = fields.get("dir") or route.dir
            if not directory:
                raise VibeError(
                    f"route {name!r} needs a working directory before it can run a command",
                    hint="set dir as well as cmd",
                )
            fields = {**fields, "type": RouteType.MANAGED, "dir": directory}

        # Only a port that is actually changing needs validating: the dashboard
        # submits the displayed port with every save, and while the route is
        # serving that port is, of course, in use -- by the route itself.
        new_port = fields.get("port")
        if new_port is not None and int(new_port) != route.port:
            becomes = fields.get("type", route.type)
            own_reserved = fields.get("reserve_ports", route.reserve_ports).values()
            fields["port"] = _resolve_port(
                data,
                int(new_port),
                excluding=name,
                require_free=becomes in (RouteType.MANAGED, RouteType.WORKTREE),
                also_claimed=own_reserved,
            )
        elif new_port is not None:
            fields["port"] = route.port

        updated = route.model_copy(update=fields)
        # Re-validate: model_copy bypasses validators, and a patch can produce a
        # shape the model forbids (a bookmark without a url, say).
        updated = Route.model_validate(updated.model_dump())
        data.routes[name] = updated

    # The plist and the login symlink are files, not a process: keep them true to
    # the registry whether or not the job is loaded, or turning autostart off on a
    # stopped app would leave it starting at the next login.
    if updated.managed and updated.cmd and updated.dir:
        launchd.write_plist(updated)

    caddy.reload(require_running=False)
    return route, updated


def update(name: str, **fields: Any) -> Route:
    """Patch an existing route in place and reload Caddy.

    Only fields present in ``fields`` change. A changed port is validated against
    the rest of the registry before anything is written. A running process is
    restarted only when the change affects it.
    """
    before, updated = _update(name, fields)
    if (
        updated.managed
        and _runtime_key(before) != _runtime_key(updated)
        and launchd.state(paths.app_label(name)).loaded
    ):
        launchd.bootstrap(updated)
    return updated


# ------------------------------------------------------------------ lifecycle


def start(name: str) -> Route:
    """Start a managed route under launchd.

    Raises:
        VibeError: if the route is not managed, or its port is taken by something
            that is not this route's own already-running job.
    """
    route = registry.get(name)
    if not route.managed:
        raise VibeError(
            f"route {name!r} is {route.type}, not a managed app",
            hint="only routes with a cmd can be started",
        )
    if route.dir and not Path(route.dir).is_dir():
        raise VibeError(f"working directory for {name!r} is gone: {route.dir}")

    job = launchd.state(paths.app_label(name))
    if job.running:
        return route

    if route.port is not None and not _wait_for_port(route.port):
        holder = ports.describe_holder(route.port)
        raise Conflict(
            f"port {route.port} is in use" + (f" by {holder}" if holder else ""),
            hint=(
                f"if it was just stopped, give it a moment; otherwise free it "
                f"or move the route: vibe-caddy update {name} --port 0"
            ),
        )

    launchd.bootstrap(route)
    return route


#: How long `start` waits for a port to come free before calling it a conflict.
PORT_RELEASE_TIMEOUT = 5.0


def _wait_for_port(port: int, timeout: float = PORT_RELEASE_TIMEOUT) -> bool:
    """Wait briefly for ``port`` to become bindable.

    `stop` returns as soon as launchd accepts the bootout, but the process it
    signalled takes a moment to exit, and a connection it had accepted can hold
    the port in TIME_WAIT for longer still. Checking once meant the obvious
    `stop` then `start` sequence reported the route's own dying process as a
    port conflict.

    Returns:
        True if the port became free within ``timeout``.
    """
    deadline = time.monotonic() + timeout
    while True:
        if ports.is_free(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def stop(name: str) -> Route:
    """Stop a managed route's launchd job, leaving the route registered."""
    route = registry.get(name)
    if not route.managed:
        raise VibeError(f"route {name!r} is {route.type}, not a managed app")
    # A job that is not loaded is already stopped, which bootout reports without
    # raising; any other refusal means the process is still running.
    launchd.bootout(paths.app_label(name))
    return route


def restart(name: str) -> Route:
    """Restart a managed route, re-reading its vibe-caddy.toml first.

    Editing the command and restarting is the obvious loop, so the project file
    is consulted again rather than the copy taken when the route was registered.
    The assigned port is kept: changing it would move the app out from under the
    Caddyfile for no reason the user asked for.
    """
    route = registry.get(name)
    if not route.managed:
        raise VibeError(f"route {name!r} is {route.type}, not a managed app")

    route = _refresh_from_project(route)
    # bootstrap already boots out whatever is loaded and waits for it to go, so
    # this is the one lifecycle transition; refreshing above only stored config.
    launchd.bootstrap(route)
    return route


def _refresh_from_project(route: Route) -> Route:
    """Re-read the route's vibe-caddy.toml, returning the updated route.

    A missing or unreadable file is not an error: a route registered inline with
    `vibe-caddy register` has no project file at all, and one whose file has
    been broken mid-edit should still be restartable with its last good command.
    """
    if not route.dir:
        return route
    config_path = Path(route.dir) / project.CONFIG_NAME
    if not config_path.is_file():
        return route
    try:
        config = project.load(config_path)
    except VibeError:
        return route

    changes = {
        "cmd": config.cmd,
        "icon": config.icon,
        "framework": config.framework,
        "autostart": config.autostart,
        "ws_origin_rewrite": config.ws_origin_rewrite,
    }
    if all(getattr(route, key) == value for key, value in changes.items()):
        return route
    return _update(route.name, changes)[1]


# --------------------------------------------------------------- vibe-caddy.toml


def start_project(directory: Path | None = None, *, as_slug: str | None = None) -> Route:
    """Register and start the project in ``directory`` from its ``vibe-caddy.toml``.

    In a linked git worktree the route is named ``<slug>.<app>``, where ``app``
    comes from the *main* checkout's config. Reading the name from the main
    checkout means a copied ``vibe-caddy.toml`` cannot silently detach a worktree from
    its parent, which would break dashboard grouping.
    """
    config_path = project.find(directory)
    if config_path is None:
        legacy = project.find_legacy(directory)
        if legacy is not None:
            raise NotFound(
                f"found {legacy}, which was renamed in this version",
                hint=f"mv {legacy} {legacy.with_name(project.CONFIG_NAME)}",
            )
        where = (directory or Path.cwd()).resolve()
        raise NotFound(
            f"no {project.CONFIG_NAME} in {where} or any parent",
            hint="create one with: vibe-caddy init",
        )

    config = project.load(config_path)
    root = config_path.parent

    main = gitwt.main_checkout(root)
    if main is None:
        return _start_or_refresh(config, root, config.name, parent=None)

    app_config = config
    main_config_path = main / project.CONFIG_NAME
    if main_config_path.is_file():
        app_config = project.load(main_config_path)

    app_name = names.validate(app_config.name)
    slug = gitwt.slug_for(root, as_slug)
    route_name = f"{slug}.{app_name}"

    return _start_or_refresh(
        config,
        root,
        route_name,
        parent=app_name,
        route_type=RouteType.WORKTREE,
        # A worktree never inherits a pinned port: siblings would collide.
        force_auto_port=True,
    )


def _start_or_refresh(
    config: ProjectConfig,
    root: Path,
    route_name: str,
    *,
    parent: str | None,
    route_type: RouteType = RouteType.MANAGED,
    force_auto_port: bool = False,
) -> Route:
    """Register the route if new, refresh it from vibe-caddy.toml if it exists, then start."""
    canonical = names.validate(route_name)
    existing = registry.load().routes.get(canonical)

    if existing is None:
        route = register(
            canonical,
            port=None if force_auto_port else config.port,
            cmd=config.cmd,
            directory=root,
            icon=config.icon,
            framework=config.framework,
            autostart=config.autostart,
            ws_origin_rewrite=config.ws_origin_rewrite,
            reserve_ports=config.reserve_ports,
            route_type=route_type,
            parent=parent,
        )
    else:
        # The name alone does not make it the same project: two checkouts can both
        # say name = "web", and refreshing would repoint the first one's route (and
        # restart its process) at the second.
        if existing.dir and Path(existing.dir).resolve() != root.resolve():
            raise Conflict(
                f"route {canonical!r} already belongs to the project in {existing.dir}",
                hint=(
                    f"rename this project in {project.CONFIG_NAME}, or free the name with: "
                    f"vibe-caddy deregister {canonical}"
                ),
            )
        # Pick up edits to vibe-caddy.toml without discarding the assigned port.
        route = update(
            canonical,
            cmd=config.cmd,
            dir=str(root),
            icon=config.icon,
            framework=config.framework,
            autostart=config.autostart,
            ws_origin_rewrite=config.ws_origin_rewrite,
        )

    start(canonical)
    return route


def prune_worktrees() -> list[str]:
    """Drop worktree routes whose checkout no longer exists.

    The parent directory is checked too: an unmounted volume makes every path
    under it vanish at once, and pruning every route on a disconnected disk would
    be destructive rather than tidy.
    """
    removed: list[str] = []
    for route in list(registry.load().routes.values()):
        if route.type is not RouteType.WORKTREE or not route.dir:
            continue
        path = Path(route.dir)
        if not (path / ".git").exists() and path.parent.is_dir():
            deregister(route.name)
            removed.append(route.name)
    return removed


# ---------------------------------------------------------------- init


@dataclass(frozen=True, slots=True)
class InitResult:
    """What ``init`` did.

    Attributes:
        name: The route name written to the file.
        path: The ``vibe-caddy.toml`` that was written.
        preset: The framework preset used, if any.
        route: The started route, or None when the project was only scaffolded.
    """

    name: str
    path: Path
    preset: frameworks.Framework | None
    route: Route | None


def init_project(
    directory: Path | None = None,
    *,
    name: str | None = None,
    framework: str | None = None,
    cmd: str | None = None,
    overwrite: bool = False,
    observe: progress.Observer = progress.ignore,
) -> InitResult:
    """Scaffold ``vibe-caddy.toml`` in ``directory``, and start it when a preset was asked for.

    Progress is reported through ``observe`` rather than only in the result because
    each line is one the user should see even if a later step fails: a framework that
    was detected and a file that was written are true whether or not the start works.

    Args:
        directory: The project; defaults to the current directory.
        name: Route name; defaults to the directory name.
        framework: A preset name, ``"auto"`` to detect one, or None for a bare scaffold.
        cmd: Start command; beats the preset's.
        overwrite: Replace an existing file.
        observe: Receives ``detected``, ``scaffolded`` and ``note`` steps.

    Raises:
        VibeError: for a missing directory, an unknown or undetectable framework, an
            existing file, or a failed start.
    """
    # Resolved before anything is written: the path ends up in the launchd job
    # as its working directory, and a relative one would be interpreted against
    # launchd's cwd rather than the shell's.
    root = (directory or Path.cwd()).resolve()
    if not root.is_dir():
        raise VibeError(
            f"{root} is not a directory",
            hint="create it first, or point --directory somewhere else",
        )

    resolved = names.validate(names.slugify(name or root.name))

    preset: frameworks.Framework | None = None
    if framework and framework != "auto":
        preset = frameworks.get(framework)
    else:
        preset = frameworks.detect(root)
        if preset is not None:
            observe(progress.Step("detected", preset.label))
        elif framework == "auto":
            raise VibeError(
                f"no framework detected in {root}",
                hint="name one explicitly, or see: vibe-caddy init --framework list",
            )

    # An explicit --cmd always wins; it is the user telling us directly.
    if cmd:
        command = cmd
    elif preset:
        command = frameworks.render_cmd(preset.cmd, paths.hostname(resolved))
    else:
        command = "npm run dev"

    # The icon placeholder stays generic: it is an emoji or an image URL, and a
    # framework name there would read as a valid value.
    path = project.scaffold(
        root,
        resolved,
        command,
        framework=preset.name if preset else None,
        overwrite=overwrite,
    )
    observe(progress.Step("scaffolded", str(path)))

    if preset is not None:
        for note in preset.notes:
            observe(progress.Step("note", note))

    # Starting is what distinguishes --framework from a bare scaffold: the
    # preset is known-good, so there is nothing for the user to edit first.
    route = start_project(root) if framework is not None else None
    return InitResult(name=resolved, path=path, preset=preset, route=route)


@dataclass(frozen=True, slots=True)
class FrameworkRow:
    """One preset as ``init --framework list`` shows it."""

    name: str
    label: str
    command: str


def framework_rows() -> list[FrameworkRow]:
    """Every framework preset with the command it generates, ordered for display."""
    return [
        FrameworkRow(
            preset.name, preset.label, frameworks.render_cmd(preset.cmd, "<name>.localhost")
        )
        for preset in sorted(frameworks.REGISTRY.values(), key=lambda f: (f.label.lower(), f.name))
    ]


# ------------------------------------------------------------- inspection


def get(name: str) -> Route:
    """Return one registered route.

    Raises:
        NotFound: if no route has that name.
    """
    return registry.get(name)


@dataclass(frozen=True, slots=True)
class Overview:
    """Everything ``status`` reports: Caddy's health and the route roll-up."""

    foreign: str | None
    running: bool
    caddy_version: str | None
    routes: list[RouteStatus]

    @property
    def serving(self) -> int:
        """How many routes are answering."""
        return sum(1 for item in self.routes if item.state in ("ready", "up"))


def overview() -> Overview:
    """Collect Caddy's state and every route's live state."""
    foreign = caddy.foreign_instance()
    running = caddy.is_running() and foreign is None
    return Overview(
        foreign=foreign,
        running=running,
        caddy_version=caddy.version(),
        routes=statuses(),
    )


def route_count() -> int:
    """How many routes are registered."""
    return len(registry.load().routes)


def route_hostnames(*, include_bookmarks: bool) -> set[str]:
    """Hostnames of the registered routes.

    Args:
        include_bookmarks: Bookmarks name an upstream we merely redirect or proxy to, so
            callers deciding whom to *trust* leave them out.

    Raises:
        VibeError: if the registry is unreadable.
    """
    return {
        route.hostname
        for route in registry.load().routes.values()
        if include_bookmarks or route.type is not RouteType.BOOKMARK
    }


def get_view() -> Literal["list", "grid"]:
    """The dashboard's saved layout, ``"list"`` or ``"grid"``."""
    return registry.load().preferences.view


def set_view(view: Literal["list", "grid"]) -> Literal["list", "grid"]:
    """Save the dashboard's layout and return it."""
    with registry.transaction() as data:
        data.preferences.view = view
    return view


@dataclass(frozen=True, slots=True)
class LogResult:
    """A route's log.

    Attributes:
        path: Where the log lives, whether or not it exists yet.
        found: False when there is no log file.
        text: The tail, or None when it was streamed to the terminal (``follow``).
    """

    path: Path
    found: bool
    text: str | None


def read_log(
    name: str, lines: int = 50, *, follow: bool = False, registered_only: bool = False
) -> LogResult:
    """Tail a route's log; the CLI and the dashboard both read logs through here.

    Args:
        name: The route.
        lines: How many trailing lines.
        follow: Stream new output to the terminal until interrupted.
        registered_only: Refuse a name that is not registered. The dashboard wants a
            404 rather than an empty log; the CLI still reads the log of a route that
            has since been deregistered.

    Raises:
        NotFound: with ``registered_only``, if no route has that name.
    """
    if registered_only:
        registry.get(name)
    log = paths.route_log(name)
    if not log.exists():
        return LogResult(log, False, "")
    if follow:
        subprocess.run(["tail", "-n", str(lines), "-f", str(log)], check=False)
        return LogResult(log, True, None)
    return LogResult(log, True, launchd.tail_log(name, lines))


def open_route(name: str | None = None, *, observe: progress.Observer = progress.ignore) -> str:
    """Open a route (default: the dashboard) in the default browser.

    Args:
        name: The route; None opens the dashboard.
        observe: Receives an ``opening`` step with the URL just before the browser is
            asked, so the URL is shown even if the launch hangs.

    Raises:
        NotFound: if ``name`` is not registered.
    """
    target = paths.url(name) if name else paths.url(paths.DASHBOARD_ROUTE)
    if name:
        registry.get(name)
    observe(progress.Step("opening", target))
    subprocess.run(["open", target], check=False, timeout=30)
    return target


def removal_targets(name: str | None, *, include_dashboard: bool = False) -> list[Route]:
    """The routes a ``deregister`` would remove: one by name, or every removable one.

    Raises:
        NotFound: if ``name`` is given and not registered.
    """
    if name is not None:
        return [registry.get(name)]
    return removable(include_dashboard=include_dashboard)


# ------------------------------------------------------------------ caddy


@dataclass(frozen=True, slots=True)
class CaddyfileResult:
    """The regenerated Caddyfile, and whether Caddy was asked to validate it."""

    content: str
    validated: bool


def write_caddyfile(*, validate: bool = False) -> CaddyfileResult:
    """Regenerate the Caddyfile from the registry, optionally validating it with Caddy.

    Raises:
        VibeError: if ``validate`` is set and Caddy rejects the file.
    """
    data = registry.load()
    content = caddy.write(data, caddy.dashboard_port_of(data))
    if validate:
        caddy.validate()
    return CaddyfileResult(content, validate)


def reload_caddy() -> None:
    """Regenerate the Caddyfile and reload the running Caddy.

    Raises:
        VibeError: if Caddy is not running or is not ours.
    """
    caddy.reload()
