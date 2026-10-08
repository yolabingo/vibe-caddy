"""Diagnostics: what is wrong, and what to run to fix it."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from . import caddy, install, paths, ports, registry, service


class Level(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: Level
    detail: str
    fix: str | None = None

    @property
    def healthy(self) -> bool:
        return self.level is not Level.FAIL


def _caddy_installed() -> Check:
    if shutil.which("caddy") is None:
        return Check("caddy binary", Level.FAIL, "not found on PATH", "brew install caddy")
    return Check("caddy binary", Level.OK, caddy.version() or "installed")


def _daemon() -> Check:
    if not paths.caddy_daemon_plist().exists():
        return Check(
            "caddy daemon",
            Level.FAIL,
            f"{paths.caddy_daemon_plist()} is missing",
            "sudo vibe-caddy setup",
        )
    if not install.daemon_loaded():
        return Check(
            "caddy daemon",
            Level.FAIL,
            "plist exists but the job is not loaded",
            f"sudo launchctl bootstrap system {paths.caddy_daemon_plist()}",
        )
    return Check("caddy daemon", Level.OK, "loaded")


def _daemon_config_path() -> Check:
    """Confirm the installed daemon reads the Caddyfile this CLI writes.

    ``sudo`` resets the environment by default, so a user who redirects
    ``XDG_STATE_HOME`` gets one path when ``vibe-caddy setup`` runs as root and a
    different one from every later unprivileged command. The symptom is silent --
    reloads appear to succeed while the daemon serves a stale file -- so the two
    paths are compared directly.
    """
    import plistlib

    plist_path = paths.caddy_daemon_plist()
    if not plist_path.exists():
        return Check("daemon config path", Level.OK, "no daemon installed yet")
    try:
        plist = plistlib.loads(plist_path.read_bytes())
        argv = list(plist.get("ProgramArguments", []))
    except (OSError, ValueError) as exc:
        return Check("daemon config path", Level.WARN, f"cannot read {plist_path}: {exc}")

    configured = argv[argv.index("--config") + 1] if "--config" in argv else None
    expected = str(paths.caddyfile())
    if configured == expected:
        return Check("daemon config path", Level.OK, expected)
    return Check(
        "daemon config path",
        Level.FAIL,
        f"daemon reads {configured}, but this CLI writes {expected}",
        "sudo --preserve-env=XDG_STATE_HOME,XDG_DATA_HOME vibe-caddy setup",
    )


def _admin_api() -> Check:
    foreign = caddy.foreign_instance()
    if foreign:
        return Check(
            "caddy admin api",
            Level.FAIL,
            f"{paths.CADDY_ADMIN} is held by {foreign}, not by vibe-caddy's Caddy",
            "stop that instance, then: sudo vibe-caddy setup",
        )
    if caddy.is_running():
        return Check("caddy admin api", Level.OK, f"responding on {paths.CADDY_ADMIN}")
    return Check(
        "caddy admin api",
        Level.FAIL,
        f"no response on {paths.CADDY_ADMIN}",
        "check the log: tail -f " + str(paths.log_dir() / "caddy.err.log"),
    )


def _listeners() -> Iterator[Check]:
    """Confirm Caddy, and not something else, holds 80 and 443."""
    for port in install.REQUIRED_PORTS:
        if not ports.is_listening(port):
            yield Check(
                f"port {port}",
                Level.FAIL,
                "nothing is listening",
                "sudo vibe-caddy setup",
            )
            continue
        holder = ports.describe_holder(port)
        if holder and "caddy" not in holder.lower():
            yield Check(
                f"port {port}",
                Level.FAIL,
                f"held by {holder}, not caddy",
                f"stop it, then: sudo launchctl kickstart -k system/{paths.CADDY_LABEL}",
            )
        else:
            yield Check(f"port {port}", Level.OK, f"caddy is listening ({holder or 'caddy'})")


def _resolution() -> Check:
    """Confirm the OS resolves a ``.localhost`` name to loopback.

    This needs no setup on macOS, so a failure here points at something unusual:
    a DNS proxy intercepting the TLD, or a ``/etc/hosts`` entry overriding it.
    """
    import socket

    probe = f"vibe-doctor-probe.{paths.TLD}"
    try:
        infos = socket.getaddrinfo(probe, None)
    except socket.gaierror as exc:
        return Check(
            "name resolution",
            Level.FAIL,
            f"{probe} does not resolve ({exc})",
            "check for a DNS proxy or a conflicting /etc/hosts entry",
        )

    addresses = {str(info[4][0]) for info in infos}
    if addresses <= {"127.0.0.1", "::1"}:
        return Check(
            "name resolution", Level.OK, f"*.{paths.TLD} resolves to {', '.join(sorted(addresses))}"
        )
    return Check(
        "name resolution",
        Level.FAIL,
        f"{probe} resolves to {', '.join(sorted(addresses))}, not loopback",
        "remove the DNS override sending .localhost elsewhere",
    )


def _ca() -> Check:
    if not paths.caddy_root_ca().exists():
        return Check(
            "tls ca",
            Level.WARN,
            "Caddy has not generated its CA yet",
            "visit any https://<name>.localhost once, then: sudo vibe-caddy setup",
        )
    if install.ca_is_trusted():
        return Check("tls ca", Level.OK, "trusted in the System keychain")
    return Check("tls ca", Level.FAIL, "not trusted; browsers will warn", "sudo vibe-caddy setup")


def _caddyfile() -> Check:
    path = paths.caddyfile()
    if not path.exists():
        return Check("caddyfile", Level.FAIL, f"{path} is missing", "vibe-caddy reload")
    try:
        caddy.validate(str(path))
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the user
        return Check("caddyfile", Level.FAIL, str(exc), "vibe-caddy reload")
    return Check("caddyfile", Level.OK, f"valid ({path})")


def _routes() -> Iterator[Check]:
    """Report managed routes whose job is loaded but not serving."""
    for status in service.statuses():
        if status.state == "crashed":
            yield Check(
                f"route {status.route.name}",
                Level.WARN,
                "launchd job is loaded but the process is not running",
                f"vibe-caddy logs {status.route.name}",
            )
        elif status.state == "starting":
            yield Check(
                f"route {status.route.name}",
                Level.WARN,
                f"process is up but nothing is listening on :{status.route.port}",
                f"confirm the command binds $PORT: vibe-caddy logs {status.route.name}",
            )


def _legacy_layout() -> Iterator[Check]:
    """Point out a pre-XDG directory left over from an older release."""
    legacy = paths.legacy_dir()
    if legacy.is_dir() and (legacy / "registry.json").exists():
        yield Check(
            "legacy layout",
            Level.WARN,
            f"{legacy} still holds a registry",
            "sudo vibe-caddy setup   (migrates it, then the old directory can be deleted)",
        )


def _stale_registry() -> Iterator[Check]:
    """Warn about worktree routes whose checkout has been removed."""
    from pathlib import Path

    from .models import RouteType

    for route in registry.load().routes.values():
        if route.type is RouteType.WORKTREE and route.dir:
            path = Path(route.dir)
            if not (path / ".git").exists() and path.parent.is_dir():
                yield Check(
                    f"route {route.name}",
                    Level.WARN,
                    f"worktree is gone: {route.dir}",
                    "vibe-caddy prune",
                )


def run() -> list[Check]:
    """Run every diagnostic, in the order a failure would cascade."""
    checks = [_caddy_installed(), _resolution(), _daemon(), _daemon_config_path(), _admin_api()]
    checks.extend(_listeners())
    checks.append(_caddyfile())
    checks.append(_ca())
    checks.extend(_routes())
    checks.extend(_legacy_layout())
    checks.extend(_stale_registry())
    return checks
