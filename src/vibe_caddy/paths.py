"""Filesystem locations used by vibe-caddy.

Layout follows the XDG Base Directory specification, honouring ``XDG_DATA_HOME``
and ``XDG_STATE_HOME`` when they are set and falling back to the spec's defaults.

That is the convention for a standalone command line tool on macOS, even though
``~/Library/Application Support`` is the platform's own: Apple's location is for
GUI applications and the CLIs that ship alongside them, and unlike the XDG
variables it cannot be redirected by the user. ``gh``, ``uv``, ``gcloud`` and
``helm`` all take the same approach.

The split is by durability, not by filename:

``XDG_DATA_HOME/vibe-caddy``
    Things that cannot be regenerated: the registry, and Caddy's local CA.
``XDG_STATE_HOME/vibe-caddy``
    Everything derived from them -- the Caddyfile, the launchd plists, the logs
    and the lock file. Safe to delete; the next command rebuilds it.

Two locations are dictated by launchd rather than chosen:
``/Library/LaunchDaemons`` for the root Caddy job, and ``~/Library/LaunchAgents``
for the opt-in start-at-login symlink.
"""

from __future__ import annotations

import os
from pathlib import Path

#: launchd label prefix for every job vibe creates.
LABEL_PREFIX = "dev.vibe-caddy"

#: Label of the root LaunchDaemon that runs Caddy.
CADDY_LABEL = f"{LABEL_PREFIX}.caddy"

#: Route name reserved for the dashboard.
DASHBOARD_ROUTE = "vibe"  # served at https://vibe.vc.localhost

#: The hostname suffix vibe serves. macOS resolves it to loopback without DNS setup.
TLD = "vc.localhost"

#: Caddy's admin API. Loopback-only; used for config reloads.
CADDY_ADMIN = "127.0.0.1:2019"

#: Loopback-only listener that answers Caddy's on-demand-TLS ``ask`` probe.
CADDY_ASK_PORT = 2021

#: Interfaces Caddy binds. Loopback only: dev servers stay off the LAN.
BIND_HOSTS = ("127.0.0.1", "::1")

#: Inclusive range scanned when auto-assigning a port to an app.
PORT_RANGE = (3000, 3999)


def home() -> Path:
    """Return the real user's home, even under ``sudo``.

    ``vibe-caddy setup`` runs as root but must read and write the invoking user's
    ``~/.vibe-caddy``; ``$HOME`` still points at the user's home under ``sudo`` on macOS,
    but ``$SUDO_USER`` is the authoritative signal when it does not.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        return Path(f"/Users/{sudo_user}")
    return Path.home()


def real_uid() -> int:
    """Return the invoking user's uid, seeing through ``sudo``.

    Per-app jobs belong in the user's GUI domain. Using ``os.getuid()`` under
    ``sudo`` would address ``gui/0``, which does not exist -- launchd rejects it
    with "Domain does not support specified action".
    """
    sudo_uid = os.environ.get("SUDO_UID")
    if sudo_uid and os.geteuid() == 0:
        return int(sudo_uid)
    return os.getuid()


def user_shell() -> str:
    """Return the invoking user's login shell.

    Read from the password database rather than ``$SHELL``: under ``sudo`` that
    variable holds root's shell (``/bin/sh``), and a job defined during
    ``vibe-caddy setup`` would then run its command in a shell that never reads
    the user's profile -- so nvm, pyenv and rbenv shims would be missing from
    PATH for that app only.
    """
    import pwd

    try:
        shell = pwd.getpwuid(real_uid()).pw_shell
    except KeyError:
        shell = ""
    return shell or os.environ.get("SHELL") or "/bin/zsh"


def _xdg(variable: str, default: str) -> Path:
    """Resolve an XDG base directory.

    A relative value is ignored, as the specification requires. ``home()`` is used
    for the fallback rather than ``Path.home()`` so the directory still resolves to
    the invoking user's when running under ``sudo``.
    """
    value = os.environ.get(variable)
    if value and value.startswith("/"):
        return Path(value)
    return home() / default


def data_dir() -> Path:
    """Durable state: the registry and Caddy's certificate authority."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "vibe-caddy"


def state_dir() -> Path:
    """Derived state: generated config, plists, logs and locks."""
    return _xdg("XDG_STATE_HOME", ".local/state") / "vibe-caddy"


def legacy_dir() -> Path:
    """Where releases before the XDG layout kept everything."""
    return home() / ".vibe-caddy"


def registry_file() -> Path:
    return data_dir() / "registry.json"


def caddyfile() -> Path:
    return state_dir() / "Caddyfile"


def caddy_data_dir() -> Path:
    """Shared Caddy data dir.

    Caddy stores its internal CA here. The root LaunchDaemon and the unprivileged
    ``caddy trust`` invocation must agree on this path or the browser would be asked
    to trust a different CA than the one signing the leaf certificates, so it is
    pinned via ``XDG_DATA_HOME`` rather than left to the per-user default.
    """
    return data_dir() / "caddy"


def caddy_root_ca() -> Path:
    return caddy_data_dir() / "caddy" / "pki" / "authorities" / "local" / "root.crt"


def launchd_dir() -> Path:
    """Where vibe writes its per-app plists.

    Deliberately not ``~/Library/LaunchAgents``: plists there are bootstrapped at
    login, which would start every registered app on every boot. Jobs here are
    bootstrapped by path on demand, and only opt-in apps get a LaunchAgents symlink.
    """
    return state_dir() / "launchd"


def launch_agents_dir() -> Path:
    return home() / "Library" / "LaunchAgents"


def log_dir() -> Path:
    return state_dir() / "log"


def route_log(name: str) -> Path:
    return log_dir() / f"{name}.log"


def caddy_log() -> Path:
    return log_dir() / "caddy.log"


def caddy_daemon_plist() -> Path:
    return Path("/Library/LaunchDaemons") / f"{CADDY_LABEL}.plist"


def app_plist(name: str) -> Path:
    return launchd_dir() / f"{LABEL_PREFIX}.{name}.plist"


def app_label(name: str) -> str:
    return f"{LABEL_PREFIX}.{name}"


def agent_symlink(name: str) -> Path:
    return launch_agents_dir() / f"{LABEL_PREFIX}.{name}.plist"


def hostname(name: str) -> str:
    return f"{name}.{TLD}"


def url(name: str) -> str:
    return f"https://{hostname(name)}"


def ensure_dirs() -> None:
    """Create the directory tree vibe writes into. Safe to call repeatedly."""
    for path in (data_dir(), state_dir(), launchd_dir(), log_dir(), caddy_data_dir()):
        path.mkdir(parents=True, exist_ok=True)
