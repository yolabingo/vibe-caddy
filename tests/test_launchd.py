"""launchd plist generation and job-state parsing; ``launchctl`` is never invoked."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from vibe_caddy import launchd, paths
from vibe_caddy.errors import VibeError
from vibe_caddy.models import Route, RouteType


def managed(**overrides: object) -> Route:
    fields: dict[str, object] = {
        "name": "web",
        "type": RouteType.MANAGED,
        "port": 3000,
        "cmd": "npm run dev",
        "dir": "/tmp/web",
        "reserve_ports": {"ws": 3001, "db_admin": 3002},
    }
    fields.update(overrides)
    return Route.model_validate(fields)


def test_build_plist_contents() -> None:
    plist = launchd.build_plist(managed(), shell="/bin/zsh")
    assert plist["Label"] == "dev.vibe-caddy.web"
    assert plist["ProgramArguments"] == ["/bin/zsh", "-lc", "npm run dev"]
    assert plist["WorkingDirectory"] == "/tmp/web"
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(paths.route_log("web"))
    assert plist["EnvironmentVariables"] == {
        "PORT": "3000",
        "VIBE_ROUTE": "web",
        "VIBE_URL": "https://web.vc.localhost",
        "VIBE_HOSTNAME": "web.vc.localhost",
        "PORT_WS": "3001",
        "PORT_DB_ADMIN": "3002",
    }


def test_build_plist_defaults_to_the_users_login_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "user_shell", lambda: "/bin/fish")
    args = launchd.build_plist(managed())["ProgramArguments"]
    assert isinstance(args, list)
    assert args[0] == "/bin/fish"


def test_worktree_environment_uses_the_full_vc_hostname() -> None:
    route = managed(name="feat.web", type=RouteType.WORKTREE, parent="web")
    environment = launchd.build_plist(route, shell="/bin/zsh")["EnvironmentVariables"]
    assert isinstance(environment, dict)
    assert environment["VIBE_ROUTE"] == "feat.web"
    assert environment["VIBE_URL"] == "https://feat.web.vc.localhost"
    assert environment["VIBE_HOSTNAME"] == "feat.web.vc.localhost"


@pytest.mark.parametrize("missing", ["cmd", "dir", "port"])
def test_build_plist_requires_runnable_route(missing: str) -> None:
    # model_copy skips validation, so a Route can be built that the model itself would refuse.
    route = managed().model_copy(update={missing: None})
    with pytest.raises(VibeError, match="not runnable"):
        launchd.build_plist(route)


def test_write_plist_is_parseable() -> None:
    target = launchd.write_plist(managed(), shell="/bin/zsh")
    assert target == paths.app_plist("web")
    assert plistlib.loads(target.read_bytes())["Label"] == "dev.vibe-caddy.web"
    assert not paths.agent_symlink("web").is_symlink()


def test_autostart_creates_symlink_and_flip_removes_it() -> None:
    target = launchd.write_plist(managed(autostart=True))
    link = paths.agent_symlink("web")
    assert link.is_symlink()
    assert link.resolve() == target.resolve()

    # Rewriting while already enabled must replace the link rather than fail.
    launchd.write_plist(managed(autostart=True))
    assert link.is_symlink()

    launchd.write_plist(managed(autostart=False))
    assert not link.is_symlink()
    assert not link.exists()


def test_remove_plist_removes_file_and_link() -> None:
    launchd.write_plist(managed(autostart=True))
    launchd.remove_plist("web")
    assert not paths.app_plist("web").exists()
    assert not paths.agent_symlink("web").is_symlink()
    launchd.remove_plist("web")  # idempotent


def fake_print(stdout: str, returncode: int = 0):
    def _run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        assert args[0] == "print"
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    return _run


RUNNING = """\
dev.vibe-caddy.web = {
\tstate = running
\tpid = 4242
\tlast exit status = 0
}
"""
CRASHED = """\
dev.vibe-caddy.web = {
\tstate = waiting
\tlast exit code = 256
}
"""
NEGATIVE = "\tstate = waiting\n\tlast exit status = -9\n"


@pytest.mark.parametrize(
    ("stdout", "pid", "exit_status", "running"),
    [
        (RUNNING, 4242, 0, True),
        (CRASHED, None, 256, False),
        (NEGATIVE, None, -9, False),
        ("\tstate = waiting\n", None, None, False),
    ],
)
def test_state_parsing(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    pid: int | None,
    exit_status: int | None,
    running: bool,
) -> None:
    monkeypatch.setattr(launchd, "_run", fake_print(stdout))
    job = launchd.state("dev.vibe-caddy.web")
    assert job == launchd.JobState(loaded=True, pid=pid, last_exit_status=exit_status)
    assert job.running is running


def test_state_not_loaded_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launchd, "_run", fake_print(RUNNING, returncode=113))
    assert launchd.state("x") == launchd.JobState(loaded=False)


def test_tail_log_missing_and_truncated() -> None:
    assert launchd.tail_log("web") == ""
    paths.ensure_dirs()
    paths.route_log("web").write_text("\n".join(f"line {i}" for i in range(100)))
    assert launchd.tail_log("web", lines=3) == "line 97\nline 98\nline 99"


def test_tail_log_tolerates_invalid_utf8() -> None:
    paths.ensure_dirs()
    paths.route_log("web").write_bytes(b"ok \xff\xfe bytes\n")
    assert "ok" in launchd.tail_log("web")


def test_bootstrap_raises_when_launchd_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def _run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 5, "", "Bootstrap failed: 5")

    monkeypatch.setattr(launchd, "_run", _run)
    with pytest.raises(VibeError, match="Bootstrap failed"):
        launchd.bootstrap(managed(), shell="/bin/zsh")
    # `print` in the middle is the wait for the previous job to unload.
    assert [c[0] for c in calls] == ["bootout", "print", "bootstrap"]
    assert Path(calls[-1][2]) == paths.app_plist("web")


def test_program_arguments_use_the_users_login_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under sudo, $SHELL is root's; the plist must still name the user's shell."""
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(paths, "user_shell", lambda: "/opt/homebrew/bin/bash")
    route = Route(name="web", type=RouteType.MANAGED, port=3000, cmd="npm run dev", dir="/tmp")
    args = launchd.build_plist(route)["ProgramArguments"]
    assert isinstance(args, list)
    assert args[0] == "/opt/homebrew/bin/bash"


def test_bootstrap_waits_for_the_previous_job_to_unload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """launchd teardown is asynchronous; bootstrapping too early fails with EIO."""
    seen: list[str] = []
    loaded = iter([True, True, False])

    monkeypatch.setattr(launchd, "state", lambda label: launchd.JobState(loaded=next(loaded)))
    monkeypatch.setattr(launchd.time, "sleep", lambda seconds: None)

    def record(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        seen.append(args[0])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(launchd, "_run", record)
    launchd.bootstrap(managed())
    # The wait happens between the bootout and the bootstrap, never after it.
    assert seen == ["bootout", "bootstrap"]


def _answer(monkeypatch: pytest.MonkeyPatch, code: int, stderr: str) -> None:
    def run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, code, "", stderr)

    monkeypatch.setattr(launchd, "_run", run)


def test_bootout_treats_an_absent_job_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, 3, "Boot-out failed: 3: No such process")
    assert launchd.bootout("dev.vibe-caddy.web") is False


def test_bootout_raises_on_any_other_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, 1, "Boot-out failed: 1: Operation not permitted")
    with pytest.raises(VibeError, match="Operation not permitted"):
        launchd.bootout("dev.vibe-caddy.web")


def test_bootout_quiet_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, 1, "Operation not permitted")
    assert launchd.bootout("dev.vibe-caddy.web", quiet=True) is False
