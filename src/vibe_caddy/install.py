"""Installing and removing the Caddy LaunchDaemon, and trusting its CA.

This is the whole of vibe-caddy's system footprint, and it is deliberately small.
There is no DNS server, no ``/etc/resolver`` file and no packet-filter rule: macOS
already resolves every name under ``.vc.localhost`` to loopback, and Caddy binds 80
and 443 directly because launchd starts it as root.

Three things are installed:

1. ``/Library/LaunchDaemons/dev.vibe-caddy.caddy.plist`` -- runs Caddy as root.
2. ``$XDG_STATE_HOME/vibe-caddy/Caddyfile`` -- the configuration it loads.
3. Caddy's own CA root, added to the System keychain so browsers accept the
   certificates Caddy mints for ``*.vc.localhost``.

Uninstalling reverses exactly those three.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import pwd
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import caddy, paths, ports
from .errors import SetupRequired, VibeError

SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"

#: Ports Caddy must own for the whole scheme to work.
REQUIRED_PORTS = (80, 443)


def is_root() -> bool:
    return os.geteuid() == 0


def require_root(action: str) -> None:
    """Raise unless running as root.

    Raises:
        VibeError: telling the user the exact command to re-run.
    """
    if not is_root():
        raise VibeError(
            f"{action} needs root to write /Library/LaunchDaemons and the System keychain",
            hint=f"re-run as: sudo vibe-caddy {action}",
        )


def real_user() -> tuple[str, int, int]:
    """Return ``(name, uid, gid)`` of the invoking user, seeing through ``sudo``.

    Files created while root must end up owned by the user, or the unprivileged
    CLI could not write its own registry afterwards.
    """
    name = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    entry = pwd.getpwnam(name)
    return name, entry.pw_uid, entry.pw_gid


def chown_to_user(path: Path) -> None:
    """Recursively give a path back to the invoking user. No-op when not root."""
    if not is_root() or not path.exists():
        return
    _, uid, gid = real_user()
    os.chown(path, uid, gid)
    if path.is_dir():
        for child in path.rglob("*"):
            os.chown(child, uid, gid)


def reclaim_own_package() -> None:
    """Give back any bytecode cache root created inside our own installation.

    ``sudo vibe-caddy setup`` runs the installed package as root, and CPython
    writes ``__pycache__`` directories as it imports. Those land root-owned inside
    the uv tool directory, where they later make ``uv tool install --force`` fail
    with ``Permission denied``. Handing them back costs nothing and keeps the next
    upgrade working.
    """
    if not is_root():
        return
    import vibe_caddy

    # Every imported dependency writes its own cache too, so the whole
    # site-packages tree we were imported from is reclaimed, not just our package.
    site_packages = Path(vibe_caddy.__file__).resolve().parent.parent
    _, uid, gid = real_user()
    for cache in site_packages.rglob("__pycache__"):
        with contextlib.suppress(OSError):
            os.chown(cache, uid, gid)
            for child in cache.iterdir():
                os.chown(child, uid, gid)


# ------------------------------------------------------------------ preflight


@dataclass(frozen=True, slots=True)
class PortConflict:
    """A port vibe-caddy needs that something else already holds."""

    port: int
    holder: str

    def __str__(self) -> str:
        if self.holder:
            return f":{self.port} held by {self.holder}"
        return (
            f":{self.port} held by a process this user cannot see "
            f"(try: sudo lsof -nP -iTCP:{self.port} -sTCP:LISTEN)"
        )


def port_conflicts() -> list[PortConflict]:
    """Return the required ports that are already bound by someone else.

    Checked before anything is written. A common cause on a developer machine is
    a containerised reverse proxy publishing 80 and 443 on all interfaces, which
    would make the LaunchDaemon fail to bind and crash-loop invisibly.

    Our own running daemon legitimately holds both ports, and re-running setup is
    the documented repair for CA trust and for the daemon's config path, so a
    listener is forgiven only when all three agree: our LaunchDaemon is loaded,
    the admin API shows our configuration, and the process on the port is Caddy.
    Requiring the holder as well keeps a foreign listener (Docker) rejected even
    while our own Caddy is up.
    """
    ours = daemon_loaded() and caddy.is_ours()
    conflicts = []
    for port in REQUIRED_PORTS:
        if ports.is_free(port):
            continue
        holder = ports.describe_holder(port)
        if ours and holder == "caddy":
            continue
        conflicts.append(PortConflict(port=port, holder=holder))
    return conflicts


def docker_hint(conflicts: list[PortConflict]) -> str | None:
    """If Docker holds a required port, name the container to stop.

    Returned as a hint rather than acted on: stopping another project's stack is
    the user's decision, not ours.
    """
    if not any(
        "docker" in conflict.holder.lower() or "com.docke" in conflict.holder
        for conflict in conflicts
    ):
        return None
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return "a Docker container holds the port; stop it with `docker stop <name>`"

    for line in out.splitlines():
        name, _, port_spec = line.partition("\t")
        if ":80->" in port_spec or ":443->" in port_spec:
            return f"container {name!r} publishes it; stop it with: docker stop {name}"
    return "a Docker container holds the port; stop it with `docker stop <name>`"


def migrate_legacy() -> list[str]:
    """Move a pre-XDG ``~/.vibe-caddy`` into the data and state directories.

    Only the registry and the certificate authority are carried over. Everything
    else in the old directory was derived from them and is rebuilt by the caller's
    next reload, so copying it would just move stale absolute paths around.

    Returns:
        Human-readable descriptions of what moved; empty when there was nothing
        to do.
    """
    legacy = paths.legacy_dir()
    if not legacy.is_dir():
        return []

    paths.ensure_dirs()
    moved = []
    for source, target in (
        (legacy / "registry.json", paths.registry_file()),
        (legacy / "caddy", paths.caddy_data_dir()),
    ):
        if (
            not source.exists()
            or target.exists()
            and any(target.iterdir() if target.is_dir() else [1])
        ):
            continue
        if target.is_dir():
            target.rmdir()
        shutil.move(str(source), str(target))
        moved.append(f"{source} -> {target}")

    if moved:
        # Leave the old directory itself behind rather than deleting a tree the
        # user may have put something of their own into; it is now inert.
        (legacy / "MOVED.txt").write_text(
            "vibe-caddy now follows the XDG base directory spec.\n"
            f"registry: {paths.registry_file()}\n"
            f"state:    {paths.state_dir()}\n"
            "This directory is no longer used and can be deleted.\n"
        )
    return moved


def caddy_binary() -> str:
    """Return the absolute path to the ``caddy`` binary.

    Raises:
        SetupRequired: if Caddy is not installed.
    """
    found = shutil.which("caddy")
    if not found:
        raise SetupRequired("caddy is not installed", hint="install it with: brew install caddy")
    return found


# ------------------------------------------------------------- LaunchDaemon


def build_daemon_plist() -> dict[str, object]:
    """Render the root LaunchDaemon that runs Caddy.

    ``XDG_DATA_HOME`` is pinned so that root's Caddy and any later user-run
    ``caddy`` command share one data directory, and therefore one CA. Without it
    root would store its CA under ``/var/root`` and ``caddy trust`` would trust a
    different, unused root.
    """
    return {
        "Label": paths.CADDY_LABEL,
        "ProgramArguments": [
            caddy_binary(),
            "run",
            "--config",
            str(paths.caddyfile()),
            "--adapter",
            "caddyfile",
        ],
        "EnvironmentVariables": {
            "XDG_DATA_HOME": str(paths.caddy_data_dir()),
            "HOME": str(paths.home()),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(paths.log_dir() / "caddy.out.log"),
        "StandardErrorPath": str(paths.log_dir() / "caddy.err.log"),
        "ProcessType": "Interactive",
    }


def daemon_loaded() -> bool:
    result = subprocess.run(
        ["launchctl", "print", f"system/{paths.CADDY_LABEL}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def install_daemon() -> Path:
    """Write and bootstrap the Caddy LaunchDaemon. Requires root."""
    require_root("setup")
    target = paths.caddy_daemon_plist()
    target.write_bytes(plistlib.dumps(build_daemon_plist()))
    target.chmod(0o644)
    os.chown(target, 0, 0)

    # Replace any previous generation before loading the new one.
    subprocess.run(
        ["launchctl", "bootout", f"system/{paths.CADDY_LABEL}"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", "system", str(target)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise VibeError(
            f"launchd refused the Caddy daemon: {(result.stderr or result.stdout).strip()}",
            hint=f"inspect it: sudo launchctl print system/{paths.CADDY_LABEL}",
        )
    return target


def uninstall_daemon() -> bool:
    """Unload and delete the Caddy LaunchDaemon. Returns True if one existed."""
    require_root("uninstall")
    subprocess.run(
        ["launchctl", "bootout", f"system/{paths.CADDY_LABEL}"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    target = paths.caddy_daemon_plist()
    existed = target.exists()
    target.unlink(missing_ok=True)
    return existed


def wait_for_caddy(timeout: float = 15.0) -> bool:
    """Poll Caddy's admin API until it answers or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if caddy.is_running(timeout=1.0):
            return True
        time.sleep(0.4)
    return False


# ------------------------------------------------------------------- CA trust


def ca_is_trusted() -> bool:
    """True if Caddy's local CA is present in the System keychain."""
    root = paths.caddy_root_ca()
    if not root.exists():
        return False
    result = subprocess.run(
        ["security", "find-certificate", "-c", "Caddy Local Authority", "-a", SYSTEM_KEYCHAIN],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0 and "Caddy Local Authority" in result.stdout


def trust_ca() -> None:
    """Add Caddy's CA root to the System keychain.

    ``caddy trust`` is used when it is available because it knows where its own
    root lives; it is invoked with the pinned data directory so it operates on the
    same CA the daemon serves with. If it fails -- it is a thin wrapper that can
    be defeated by an unusual keychain setup -- the underlying ``security`` call
    is made directly against the root certificate on disk.

    Raises:
        VibeError: if the certificate still is not trusted afterwards.
    """
    require_root("setup")
    env = dict(os.environ)
    env["XDG_DATA_HOME"] = str(paths.caddy_data_dir())
    env["HOME"] = str(paths.home())

    subprocess.run(
        [caddy_binary(), "trust"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    if ca_is_trusted():
        return

    root = paths.caddy_root_ca()
    if not root.exists():
        raise VibeError(
            f"Caddy has not generated its CA yet (expected {root})",
            hint="make one request to any https://<name>.vc.localhost, then re-run setup",
        )
    result = subprocess.run(
        [
            "security",
            "add-trusted-cert",
            "-d",
            "-r",
            "trustRoot",
            "-k",
            SYSTEM_KEYCHAIN,
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise VibeError(f"could not trust the Caddy CA: {result.stderr.strip()}")


def untrust_ca() -> bool:
    """Remove Caddy's CA from the System keychain. Returns True if one was removed."""
    require_root("uninstall")
    removed = False
    # The same CN can be present more than once after repeated installs.
    while ca_is_trusted():
        result = subprocess.run(
            ["security", "delete-certificate", "-c", "Caddy Local Authority", SYSTEM_KEYCHAIN],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            break
        removed = True
    return removed
