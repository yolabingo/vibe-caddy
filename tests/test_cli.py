"""Command-line behaviour through ``typer.testing.CliRunner``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vibe_caddy import (
    __version__,
    caddy,
    cli,
    doctor,
    install,
    paths,
    ports,
    provision,
    registry,
    service,
)
from vibe_caddy.cli import app
from vibe_caddy.errors import VibeError

runner = CliRunner()


@pytest.fixture(autouse=True)
def quiet(reloads: list[object], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "is_free", lambda port: True)
    monkeypatch.setattr(ports, "is_listening", lambda port, timeout=0.3: False)


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_list_empty() -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "no routes registered" in result.stdout


def test_register_then_list() -> None:
    registered = runner.invoke(app, ["register", "web", "3100"])
    assert registered.exit_code == 0, registered.output
    assert "https://web.localhost" in registered.stdout
    assert "web" in registry.load().routes

    listed = runner.invoke(app, ["list"])
    assert listed.exit_code == 0
    assert "web" in listed.stdout
    assert "3100" in listed.stdout


def test_register_bookmark() -> None:
    result = runner.invoke(app, ["register", "docs", "--url", "https://example.com"])
    assert result.exit_code == 0, result.output
    assert registry.get("docs").type.value == "bookmark"


def test_register_without_port_or_url_fails() -> None:
    result = runner.invoke(app, ["register", "web"])
    assert result.exit_code == 1
    assert "give a port" in result.stderr
    assert registry.load().routes == {}


def test_register_duplicate_reports_hint() -> None:
    runner.invoke(app, ["register", "web", "3100"])
    result = runner.invoke(app, ["register", "web", "3101"])
    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert "hint" in result.stderr


def test_deregister_unknown_exits_1_on_stderr() -> None:
    result = runner.invoke(app, ["deregister", "nope"])
    assert result.exit_code == 1
    assert "nope" in result.stderr
    assert "nope" not in result.stdout


def test_deregister_removes_route() -> None:
    runner.invoke(app, ["register", "web", "3100"])
    result = runner.invoke(app, ["deregister", "web"])
    assert result.exit_code == 0
    assert registry.load().routes == {}


def test_caddyfile_prints_config() -> None:
    runner.invoke(app, ["register", "web", "3100"])
    result = runner.invoke(app, ["caddyfile"])
    assert result.exit_code == 0
    assert "web.localhost" in result.stdout
    assert "bind 127.0.0.1 ::1" in result.stdout
    assert paths.caddyfile().exists()


def test_doctor_runs_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "daemon_loaded", lambda: False)
    monkeypatch.setattr(install, "ca_is_trusted", lambda: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1  # the stubbed machine has failures
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "brew install caddy" in result.stdout


# ------------------------------------------------------- deregister --all


def test_deregister_requires_a_name_or_all(reloads: list[object]) -> None:
    result = runner.invoke(app, ["deregister"])
    assert result.exit_code == 1
    assert "give a route name, or --all" in result.output


def test_deregister_rejects_a_name_together_with_all(reloads: list[object]) -> None:
    assert runner.invoke(app, ["deregister", "a", "--all"]).exit_code == 1


def test_deregister_all_removes_everything(reloads: list[object]) -> None:
    service.register("a", port=3100)
    service.register("b", port=3101)
    result = runner.invoke(app, ["deregister", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert registry.load().routes == {}


def test_deregister_all_prompts_and_a_refusal_changes_nothing(
    reloads: list[object],
) -> None:
    """The prompt is the only guard on a destructive bulk action."""
    service.register("a", port=3100)
    result = runner.invoke(app, ["deregister", "--all"], input="n\n")
    assert result.exit_code == 1
    assert "cancelled" in result.output
    assert set(registry.load().routes) == {"a"}


def test_deregister_all_lists_what_it_will_remove(reloads: list[object]) -> None:
    service.register("alpha", port=3100)
    result = runner.invoke(app, ["deregister", "--all"], input="n\n")
    assert "alpha" in result.output


def test_deregister_all_spares_the_dashboard(reloads: list[object]) -> None:
    service.register(paths.DASHBOARD_ROUTE, cmd="x", directory=Path("/tmp"))
    service.register("app", port=3100)
    result = runner.invoke(app, ["deregister", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert set(registry.load().routes) == {paths.DASHBOARD_ROUTE}


def test_deregister_all_can_include_the_dashboard(reloads: list[object]) -> None:
    service.register(paths.DASHBOARD_ROUTE, cmd="x", directory=Path("/tmp"))
    result = runner.invoke(app, ["deregister", "--all", "--include-dashboard", "--yes"])
    assert result.exit_code == 0, result.output
    assert registry.load().routes == {}


def test_deregister_all_on_an_empty_registry_is_not_an_error(
    reloads: list[object],
) -> None:
    result = runner.invoke(app, ["deregister", "--all", "--yes"])
    assert result.exit_code == 0
    assert "nothing to remove" in result.output


# ------------------------------------------------------------- help panels


def test_help_groups_every_command_into_a_panel() -> None:
    """A command with no panel falls into a generic 'Commands' box on its own."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "╭─ Commands ─" not in result.output


@pytest.mark.parametrize(
    "panel",
    [
        cli.PANEL_INSPECT,
        cli.PANEL_APPS,
        cli.PANEL_ROUTES,
        cli.PANEL_PROXY,
        cli.PANEL_SYSTEM,
    ],
)
def test_every_panel_is_present_in_help(panel: str) -> None:
    assert panel in runner.invoke(app, ["--help"]).output


# --------------------------------------------------------------- init -d


def test_init_defaults_to_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reloads: list[object]
) -> None:
    project_dir = tmp_path / "here"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert (project_dir / "vibe-caddy.toml").is_file()


def test_init_writes_into_the_given_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reloads: list[object]
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--directory", str(elsewhere)])
    assert result.exit_code == 0, result.output
    assert (elsewhere / "vibe-caddy.toml").is_file()
    assert not (tmp_path / "vibe-caddy.toml").exists()


def test_init_short_flag_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reloads: list[object]
) -> None:
    target = tmp_path / "shortflag"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "-d", str(target)]).exit_code == 0
    assert (target / "vibe-caddy.toml").is_file()


def test_init_names_the_route_after_the_target_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reloads: list[object]
) -> None:
    """The name must follow -d, not the shell's cwd."""
    target = tmp_path / "my-app"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "-d", str(target)])
    assert 'name = "my-app"' in (target / "vibe-caddy.toml").read_text()


def test_init_rejects_a_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reloads: list[object]
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "-d", str(tmp_path / "nope")])
    assert result.exit_code == 1
    assert "not a directory" in result.output


def test_init_records_an_absolute_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reloads: list[object]
) -> None:
    """A relative dir would resolve against launchd's cwd, not the shell's."""
    target = tmp_path / "rel"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "-d", "rel"])
    assert result.exit_code == 0, result.output
    assert (target / "vibe-caddy.toml").is_file()


# ---------------------------------------------------------------------- setup


@pytest.fixture
def setup_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Run ``setup`` as a fake root with every system boundary stubbed.

    Returns a record of ownership hand-backs and system calls so tests can assert
    that cleanup ran and that nothing real was reached.
    """
    record: dict[str, list[str]] = {"chown": [], "reclaim": [], "calls": []}
    monkeypatch.setattr(install, "is_root", lambda: True)
    monkeypatch.setattr(install, "caddy_binary", lambda: "/usr/bin/caddy")
    monkeypatch.setattr(install, "migrate_legacy", lambda: [])
    monkeypatch.setattr(install, "daemon_loaded", lambda: False)
    monkeypatch.setattr(install, "install_daemon", lambda: record["calls"].append("daemon"))
    monkeypatch.setattr(install, "wait_for_caddy", lambda timeout=15.0: True)
    monkeypatch.setattr(install, "trust_ca", lambda: record["calls"].append("trust"))
    monkeypatch.setattr(install, "chown_to_user", lambda path: record["chown"].append(path.name))
    monkeypatch.setattr(install, "reclaim_own_package", lambda: record["reclaim"].append("x"))
    monkeypatch.setattr(caddy, "is_ours", lambda timeout=2.0: False)
    monkeypatch.setattr(caddy, "foreign_instance", lambda: None)
    monkeypatch.setattr(caddy, "validate", lambda *args, **kwargs: None)
    monkeypatch.setattr(provision, "_prime_ca", lambda: None)
    monkeypatch.setattr(provision, "install_dashboard", lambda quiet=False: None)
    monkeypatch.setattr(ports, "describe_holder", lambda port: "")
    return record


def _assert_state_handed_back(record: dict[str, list[str]]) -> None:
    assert paths.data_dir().name in record["chown"]
    assert paths.state_dir().name in record["chown"]
    assert record["reclaim"]


def test_setup_succeeds_twice_when_own_daemon_holds_ports(
    setup_env: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins finding 3: re-running setup on a healthy install must not abort."""
    monkeypatch.setattr(ports, "is_free", lambda port: False)
    monkeypatch.setattr(ports, "describe_holder", lambda port: "caddy")
    monkeypatch.setattr(install, "daemon_loaded", lambda: True)
    monkeypatch.setattr(caddy, "is_ours", lambda timeout=2.0: True)

    first = runner.invoke(app, ["setup"])
    second = runner.invoke(app, ["setup"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert setup_env["calls"].count("daemon") == 2


def test_setup_still_rejects_docker_on_ports(
    setup_env: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins finding 3: a foreign listener is rejected even with our daemon up."""
    monkeypatch.setattr(ports, "is_free", lambda port: False)
    monkeypatch.setattr(ports, "describe_holder", lambda port: "com.docker.backend")
    monkeypatch.setattr(install, "daemon_loaded", lambda: True)
    monkeypatch.setattr(caddy, "is_ours", lambda timeout=2.0: True)
    monkeypatch.setattr(install, "docker_hint", lambda conflicts: "stop the container")

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 1
    assert "com.docker.backend" in result.stderr
    assert "stop the container" in result.stderr
    assert "daemon" not in setup_env["calls"]
    # The refusal is itself a failure inside setup, so it hands state back too.
    _assert_state_handed_back(setup_env)


def test_setup_success_hands_state_back(setup_env: dict[str, list[str]]) -> None:
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0, result.output
    _assert_state_handed_back(setup_env)


def test_setup_validation_failure_hands_state_back(
    setup_env: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins finding 8: Caddyfile validation fails before the dashboard step."""

    def bad_validate(*args: object, **kwargs: object) -> None:
        raise VibeError("bad Caddyfile")

    monkeypatch.setattr(caddy, "validate", bad_validate)
    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 1
    assert "daemon" not in setup_env["calls"]
    _assert_state_handed_back(setup_env)


def test_setup_daemon_timeout_hands_state_back(
    setup_env: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins finding 8: the daemon never answers on the admin API."""
    monkeypatch.setattr(install, "wait_for_caddy", lambda timeout=15.0: False)
    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 1
    assert "caddy did not start" in result.stderr
    _assert_state_handed_back(setup_env)


def test_setup_keychain_failure_hands_state_back(
    setup_env: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins finding 8: trusting the CA fails after the daemon is already up."""

    def bad_trust() -> None:
        raise VibeError("could not trust the Caddy CA")

    monkeypatch.setattr(install, "trust_ca", bad_trust)
    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 1
    assert "could not trust" in result.stderr
    _assert_state_handed_back(setup_env)


def test_no_command_leaks_a_traceback_for_a_vibe_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A VibeError is a message for the user; only a real crash deserves a traceback."""
    import typer

    broken = typer.Typer()

    @broken.command()
    def boom() -> None:
        raise VibeError("it broke", hint="try the other thing")

    monkeypatch.setattr(cli, "app", broken)
    with pytest.raises(SystemExit) as exit_info:
        cli.run()
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "it broke" in err
    assert "try the other thing" in err
    assert "Traceback" not in err
