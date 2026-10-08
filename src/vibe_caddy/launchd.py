"""Process supervision via launchd.

vibe does not supervise anything itself. For each managed route it renders a job
definition and hands it to launchd, which owns restart-on-crash, log redirection
and PID tracking -- all of which the previous implementation reimplemented with
``lsof`` and ``ps`` scraping.

Plists live under ``XDG_STATE_HOME`` rather than ``~/Library/LaunchAgents`` so that
merely registering an app does not make it start at login; jobs are bootstrapped
by path on demand. Routes that opt into ``autostart`` get a symlink into
``LaunchAgents`` as well.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .errors import VibeError
from .models import Route

#: Seconds launchd waits between restarts of a job that keeps exiting.
THROTTLE_SECONDS = 10

#: How long to keep retrying a bootstrap that races a still-unloading job.
BOOTSTRAP_TIMEOUT = 5.0


def domain() -> str:
    """The launchd domain for the invoking user's GUI login session."""
    return f"gui/{paths.real_uid()}"


def service_target(label: str) -> str:
    return f"{domain()}/{label}"


def _run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, timeout=30, check=check
    )


def build_plist(route: Route, shell: str | None = None) -> dict[str, object]:
    """Render the launchd job definition for a managed route.

    The command runs under a login shell so that version managers (nvm, pyenv,
    rbenv) are on ``PATH``; launchd's own environment is almost empty, and a dev
    command that resolves to a shim in ``~/.nvm`` would otherwise not be found.
    """
    if route.port is None or not route.cmd or not route.dir:
        raise VibeError(f"route {route.name!r} is not runnable: needs cmd, dir and port")

    env: dict[str, str] = {
        "PORT": str(route.port),
        "VIBE_ROUTE": route.name,
        "VIBE_URL": paths.url(route.name),
        "VIBE_HOSTNAME": route.hostname,
    }
    for key, value in route.reserve_ports.items():
        env[f"PORT_{key.upper()}"] = str(value)

    log = str(paths.route_log(route.name))
    return {
        "Label": paths.app_label(route.name),
        "ProgramArguments": [shell or paths.user_shell(), "-lc", route.cmd],
        "WorkingDirectory": route.dir,
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        # Restart on a crash but respect a deliberate clean exit, so a one-shot
        # command that finishes successfully is not spun forever.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_SECONDS,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Interactive",
    }


def write_plist(route: Route, shell: str | None = None) -> Path:
    """Write the route's plist to disk and return its path."""
    paths.ensure_dirs()
    target = paths.app_plist(route.name)
    target.write_bytes(plistlib.dumps(build_plist(route, shell=shell)))

    link = paths.agent_symlink(route.name)
    if route.autostart:
        paths.launch_agents_dir().mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(target)
    elif link.is_symlink():
        link.unlink()
    return target


def remove_plist(name: str) -> None:
    paths.app_plist(name).unlink(missing_ok=True)
    link = paths.agent_symlink(name)
    if link.is_symlink():
        link.unlink()


def bootstrap(route: Route, shell: str | None = None) -> None:
    """Load and start the route's job.

    Raises:
        VibeError: if launchd refuses the job, quoting its message.
    """
    plist = write_plist(route, shell=shell)
    label = paths.app_label(route.name)

    # A stale job from a previous run would make bootstrap fail, so it is removed
    # first -- and waited for. launchd tears jobs down asynchronously, and
    # bootstrapping a label that is still unloading fails with "Input/output
    # error". That EIO is also what a genuinely broken job returns, so the message
    # cannot be used to tell the two apart; waiting until the job is really gone
    # removes the ambiguity instead of guessing.
    bootout(label, quiet=True)
    _wait_until_gone(label)

    result = _run("bootstrap", domain(), str(plist))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise VibeError(
            f"launchd refused to start {route.name!r}: {detail}",
            hint=f"inspect the job with: launchctl print {service_target(label)}",
        )


def _wait_until_gone(label: str, timeout: float = BOOTSTRAP_TIMEOUT) -> bool:
    """Block until launchd no longer knows about ``label``.

    Returns:
        True if the job went away, False if it was still listed at timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not state(label).loaded:
            return True
        time.sleep(0.1)
    return False


def bootout(label: str, *, quiet: bool = False) -> bool:
    """Stop and unload a job.

    Args:
        quiet: Swallow every failure. For clearing a possibly-stale job before a
            bootstrap, where the outcome is verified separately.

    Returns:
        True if a job was removed, False if there was none to remove.

    Raises:
        VibeError: if launchd refused for a reason other than the job being absent;
            the process may still be running.
    """
    result = _run("bootout", service_target(label))
    if result.returncode == 0:
        return True
    if quiet:
        return False
    detail = (result.stderr or result.stdout or "").strip()
    # ESRCH ("No such process") or an unknown service means the job simply was not
    # loaded, which is what the caller wanted anyway.
    if "No such process" in detail or "not find" in detail.lower():
        return False
    raise VibeError(
        f"launchd could not stop {label!r}: {detail or f'exit status {result.returncode}'}",
        hint=f"inspect the job with: launchctl print {service_target(label)}",
    )


def kickstart(label: str) -> None:
    """Restart a loaded job in place."""
    _run("kickstart", "-k", service_target(label))


@dataclass(frozen=True, slots=True)
class JobState:
    """What launchd currently knows about one job."""

    loaded: bool
    pid: int | None = None
    last_exit_status: int | None = None

    @property
    def running(self) -> bool:
        return self.pid is not None


_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)", re.MULTILINE)
_EXIT_RE = re.compile(r"^\s*last exit (?:code|status)\s*=\s*(-?\d+)", re.MULTILINE)


def state(label: str) -> JobState:
    """Query launchd for a job's state.

    ``launchctl print`` is the only interface that reports both the PID and the
    last exit status; its output is parsed leniently so a format change degrades
    to "loaded but unknown PID" rather than an exception.
    """
    result = _run("print", service_target(label))
    if result.returncode != 0:
        return JobState(loaded=False)

    pid_match = _PID_RE.search(result.stdout)
    exit_match = _EXIT_RE.search(result.stdout)
    return JobState(
        loaded=True,
        pid=int(pid_match.group(1)) if pid_match else None,
        last_exit_status=int(exit_match.group(1)) if exit_match else None,
    )


def tail_log(name: str, lines: int = 40) -> str:
    """Return the last ``lines`` of a route's log, or '' if there is none."""
    log = paths.route_log(name)
    if not log.exists():
        return ""
    try:
        content = log.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])
