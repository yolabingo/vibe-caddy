"""Port probing and assignment."""

from __future__ import annotations

import os

import socket
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from vibe_caddy import ports
from vibe_caddy.errors import Conflict


@contextmanager
def listening() -> Generator[int]:
    """Bind an ephemeral loopback port and yield it while it is held."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield sock.getsockname()[1]


def test_is_free_tracks_a_bound_port() -> None:
    with listening() as port:
        assert ports.is_free(port) is False
        assert ports.is_listening(port) is True
    assert ports.is_free(port) is True
    assert ports.is_listening(port) is False


def test_find_free_skips_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "PORT_RANGE", (45000, 45010))
    monkeypatch.setattr(ports, "is_free", lambda port: True)
    assert ports.find_free() == 45000
    assert ports.find_free({45000, 45001}) == 45002


def test_find_free_skips_bound_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "PORT_RANGE", (45000, 45010))
    monkeypatch.setattr(ports, "is_free", lambda port: port != 45000)
    assert ports.find_free() == 45001


def test_find_free_raises_when_range_is_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    # `ports` imported PORT_RANGE by name, so patching paths.PORT_RANGE would not apply.
    monkeypatch.setattr(ports, "PORT_RANGE", (45000, 45002))
    monkeypatch.setattr(ports, "is_free", lambda port: True)
    with pytest.raises(Conflict, match="45000-45002"):
        ports.find_free([45000, 45001, 45002])


def test_is_free_handles_privileged_ports_without_root() -> None:
    """A privileged port must not be reported as busy merely because we cannot bind it.

    Binding port 80 as an unprivileged user fails with EACCES before the kernel
    considers whether anything holds it, so a naive bind test reports every port
    below 1024 as occupied.
    """
    if os.geteuid() == 0:
        pytest.skip("needs to run unprivileged to exercise the EACCES path")
    # Nothing is asserted about the value itself -- only that the answer agrees
    # with an actual connect probe rather than with the bind failure.
    assert ports.is_free(80) is (not ports.is_listening(80))


def test_is_free_ignores_time_wait_but_not_a_live_listener() -> None:
    """A probe without SO_REUSEADDR calls a TIME_WAIT port busy for minutes.

    The question `is_free` must answer is "could the app bind this?", and an app
    sets SO_REUSEADDR, so the probe has to as well.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        assert ports.is_free(port) is False  # a live listener is a real conflict
    finally:
        listener.close()

    # Connect-then-close would leave TIME_WAIT behind; the port must read free.
    assert ports.is_free(port) is True
