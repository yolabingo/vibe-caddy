"""Diagnostics, with every external probe stubbed."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_caddy import caddy, doctor, install, paths, ports, service
from vibe_caddy.doctor import Check, Level


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/caddy")
    monkeypatch.setattr(caddy, "version", lambda: "v2.test")
    monkeypatch.setattr(caddy, "is_running", lambda timeout=2.0: False)
    monkeypatch.setattr(caddy, "validate", lambda path=None: None)
    monkeypatch.setattr(paths, "caddy_daemon_plist", lambda: tmp_path / "daemon.plist")
    monkeypatch.setattr(install, "daemon_loaded", lambda: False)
    monkeypatch.setattr(install, "ca_is_trusted", lambda: False)
    monkeypatch.setattr(ports, "is_listening", lambda port, timeout=0.3: False)
    monkeypatch.setattr(ports, "describe_holder", lambda port: "")
    monkeypatch.setattr(service, "statuses", lambda: [])


def test_run_returns_checks(stubbed: None) -> None:
    checks = doctor.run()
    assert checks
    assert all(isinstance(c, Check) for c in checks)
    names_ = [c.name for c in checks]
    assert names_[:3] == ["caddy binary", "name resolution", "caddy daemon"]
    assert {
        "caddy admin api",
        "daemon config path",
        "port 80",
        "port 443",
        "caddyfile",
        "tls ca",
    } <= set(names_)


def test_missing_caddy_binary_fails_with_brew_fix(
    stubbed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    check = doctor._caddy_installed()
    assert check.level is Level.FAIL
    assert check.fix == "brew install caddy"
    assert check.healthy is False


def test_installed_caddy_reports_version(stubbed: None) -> None:
    check = doctor._caddy_installed()
    assert (check.level, check.detail) == (Level.OK, "v2.test")


def test_resolution_ok_on_this_machine() -> None:
    # No stubbing on purpose: macOS resolving *.localhost to loopback is the premise of vibe.
    check = doctor._resolution()
    assert check.level is Level.OK, check.detail


def test_nothing_listening_fails_port_checks(stubbed: None) -> None:
    checks = {c.name: c for c in doctor._listeners()}
    assert checks["port 80"].level is Level.FAIL
    assert checks["port 443"].level is Level.FAIL


def test_foreign_listener_is_flagged(stubbed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "is_listening", lambda port, timeout=0.3: True)
    monkeypatch.setattr(ports, "describe_holder", lambda port: "nginx")
    assert all("nginx" in c.detail and c.level is Level.FAIL for c in doctor._listeners())
