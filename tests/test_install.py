"""Preflight checks in ``install`` with every system boundary stubbed."""

from __future__ import annotations

import pytest

from vibe_caddy import caddy, install, ports


@pytest.fixture
def busy_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "is_free", lambda port: False)


def _own_install(monkeypatch: pytest.MonkeyPatch, *, loaded: bool, ours: bool) -> None:
    monkeypatch.setattr(install, "daemon_loaded", lambda: loaded)
    monkeypatch.setattr(caddy, "is_ours", lambda timeout=2.0: ours)


@pytest.mark.usefixtures("busy_ports")
def test_own_caddy_is_not_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins finding 3: our loaded daemon's Caddy on 80/443 is accepted."""
    _own_install(monkeypatch, loaded=True, ours=True)
    monkeypatch.setattr(ports, "describe_holder", lambda port: "caddy")
    assert install.port_conflicts() == []


@pytest.mark.usefixtures("busy_ports")
def test_foreign_holder_rejected_beside_own_caddy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins finding 3: Docker on a port is still rejected."""
    _own_install(monkeypatch, loaded=True, ours=True)
    monkeypatch.setattr(
        ports, "describe_holder", lambda port: "com.docker.backend" if port == 443 else "caddy"
    )
    conflicts = install.port_conflicts()
    assert [c.port for c in conflicts] == [443]


@pytest.mark.usefixtures("busy_ports")
@pytest.mark.parametrize(("loaded", "ours"), [(False, True), (True, False), (False, False)])
def test_caddy_holder_rejected_unless_install_is_ours(
    monkeypatch: pytest.MonkeyPatch, loaded: bool, ours: bool
) -> None:
    """Pins finding 3: a stray caddy is not forgiven without our daemon and config."""
    _own_install(monkeypatch, loaded=loaded, ours=ours)
    monkeypatch.setattr(ports, "describe_holder", lambda port: "caddy")
    assert [c.port for c in install.port_conflicts()] == [80, 443]


def test_free_ports_no_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    _own_install(monkeypatch, loaded=False, ours=False)
    monkeypatch.setattr(ports, "is_free", lambda port: True)
    assert install.port_conflicts() == []
