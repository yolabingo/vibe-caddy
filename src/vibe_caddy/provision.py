"""Installing and removing vibe-caddy's system footprint.

``setup`` and ``uninstall`` are the orchestration the CLI used to carry itself: the
order of the steps, the refuse-before-changing-anything preflight, and the guarantee
that root-owned state is handed back whatever happens. They live here so the CLI only
parses arguments and renders what comes back.

Nothing in this module prints. Slow steps are reported to an observer as they finish
(see :mod:`vibe_caddy.progress`); a refusal is returned as data for the caller to word.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

import httpx2

from . import caddy, install, paths, progress, registry, service
from .errors import VibeError
from .models import Route, RouteType

# --------------------------------------------------------------------- setup


@dataclass(frozen=True, slots=True)
class PortsHeld:
    """Setup refused: something else holds a port vibe-caddy needs."""

    conflicts: tuple[install.PortConflict, ...]
    hint: str | None


@dataclass(frozen=True, slots=True)
class ForeignCaddy:
    """Setup refused: a Caddy vibe-caddy did not start holds the admin port."""

    holder: str


Refusal = PortsHeld | ForeignCaddy


@dataclass(frozen=True, slots=True)
class SetupResult:
    """How ``setup`` ended.

    Attributes:
        refusal: Why nothing was changed, or None when setup ran.
        dashboard_error: Why the dashboard did not start. Routing already works when
            this is set, so it is a warning, not a failure.
    """

    refusal: Refusal | None = None
    dashboard_error: str | None = None


def setup(*, skip_trust: bool = False, observe: progress.Observer = progress.ignore) -> SetupResult:
    """Install the Caddy LaunchDaemon, trust its CA and start the dashboard.

    Args:
        skip_trust: Leave the System keychain alone.
        observe: Receives ``migrated``, ``wrote``, ``loaded``, ``serving`` and
            ``trusted`` steps as each completes; the steps are slow.

    Returns:
        A refusal when a preflight check failed (nothing was changed), otherwise the
        outcome of the dashboard step.

    Raises:
        VibeError: if a step fails after the preflight.
    """
    install.require_root("setup")
    install.caddy_binary()
    # ensure_dirs is the first point where root-owned state appears, so the
    # hand-back covers everything from here on. Validation, a daemon that never
    # starts and a keychain failure all abort before the dashboard step, and
    # would otherwise leave data_dir and state_dir root-owned, breaking every
    # later unprivileged registry write.
    try:
        paths.ensure_dirs()
        return _setup_steps(skip_trust, observe)
    finally:
        install.chown_to_user(paths.data_dir())
        install.chown_to_user(paths.state_dir())
        install.reclaim_own_package()


def _preflight() -> Refusal | None:
    """Refuse before changing anything: a half-installed state is worse than none."""
    conflicts = install.port_conflicts()
    if conflicts:
        return PortsHeld(tuple(conflicts), install.docker_hint(conflicts))

    foreign = caddy.foreign_instance()
    if foreign:
        return ForeignCaddy(foreign)
    return None


def _setup_steps(skip_trust: bool, observe: progress.Observer) -> SetupResult:
    for moved in install.migrate_legacy():
        observe(progress.Step("migrated", str(moved)))

    refusal = _preflight()
    if refusal is not None:
        return SetupResult(refusal=refusal)

    data = registry.load()
    caddy.write(data, caddy.dashboard_port_of(data))
    caddy.validate()
    observe(progress.Step("wrote", str(paths.caddyfile())))

    install.install_daemon()
    observe(progress.Step("loaded", str(paths.caddy_daemon_plist())))

    if not install.wait_for_caddy():
        raise VibeError(
            "caddy did not start",
            hint=f"check: tail -n 50 {paths.log_dir() / 'caddy.err.log'}",
        )
    observe(progress.Step("serving", f"https://*.{paths.TLD} on 443 (loopback only)"))

    if not skip_trust:
        _prime_ca()
        install.trust_ca()
        observe(progress.Step("trusted", "Caddy's local CA in the System keychain"))

    # Routing already works at this point. A dashboard that will not start is
    # worth reporting, but it must not make a successful setup look like a failure.
    try:
        install_dashboard()
    except VibeError as exc:
        return SetupResult(dashboard_error=str(exc))
    return SetupResult()


def _prime_ca() -> None:
    """Make one HTTPS request so Caddy generates its CA before we trust it.

    Caddy creates the local authority lazily, on the first certificate it needs.
    Without this, a fresh install would have no root certificate to add.
    """
    with httpx2.Client(verify=False, timeout=10.0) as client:  # noqa: S501 - priming our own CA
        try:
            client.get(paths.url(paths.DASHBOARD_ROUTE))
        except httpx2.HTTPError:
            pass


def install_dashboard() -> Route:
    """Register and start the dashboard as an ordinary managed route.

    The dashboard is supervised exactly like any app the user registers, so a
    crash in it cannot take routing down with it.

    Raises:
        VibeError: if the route cannot be registered or started.
    """
    cmd = f"{sys.executable} -m vibe_caddy dashboard serve --port $PORT"
    route = service.register(
        paths.DASHBOARD_ROUTE,
        cmd=cmd,
        directory=paths.home(),
        icon="\N{HIGH VOLTAGE SIGN}",
        route_type=RouteType.MANAGED,
        replace=True,
    )
    # `register(replace=True)` has already re-bootstrapped the job if one was
    # loaded, so this only has to cover the first install.
    service.start(route.name)
    # The catch-all site block proxies to the dashboard, so its port must be in
    # the Caddyfile before unknown names can reach the "not registered" page.
    caddy.reload(require_running=False)
    install.chown_to_user(paths.data_dir())
    install.chown_to_user(paths.state_dir())
    return route


# ----------------------------------------------------------------- uninstall


@dataclass(frozen=True, slots=True)
class UninstallResult:
    """What ``uninstall`` found to remove."""

    daemon_removed: bool
    ca_untrusted: bool


def uninstall(*, observe: progress.Observer = progress.ignore) -> UninstallResult:
    """Remove the Caddy LaunchDaemon and untrust its CA.

    Args:
        observe: Receives ``removed`` and ``untrusted`` steps; the keychain step can
            be slow and can fail, so the first is reported before the second starts.

    Raises:
        VibeError: if not root, or a step fails.
    """
    install.require_root("uninstall")
    daemon_removed = install.uninstall_daemon()
    if daemon_removed:
        observe(progress.Step("removed", str(paths.caddy_daemon_plist())))
    ca_untrusted = install.untrust_ca()
    if ca_untrusted:
        observe(progress.Step("untrusted", "Caddy's local CA"))
    return UninstallResult(daemon_removed, ca_untrusted)


# ------------------------------------------------------------ daemon control


def start_daemon() -> bool:
    """Load the Caddy LaunchDaemon. Returns True once Caddy answers.

    Raises:
        VibeError: if not root, or launchd refuses the job.
    """
    install.install_daemon()
    return install.wait_for_caddy()


def stop_daemon() -> None:
    """Unload the Caddy LaunchDaemon.

    Raises:
        VibeError: if not root.
    """
    install.uninstall_daemon()


def restart_daemon() -> bool:
    """Restart the Caddy LaunchDaemon in place. Returns True once Caddy answers.

    Raises:
        VibeError: if not root.
    """
    install.require_root("caddy restart")
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"system/{paths.CADDY_LABEL}"], check=False, timeout=30
    )
    return install.wait_for_caddy()
