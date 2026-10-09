"""Port probing and assignment."""

from __future__ import annotations

import contextlib
import errno
import socket
from collections.abc import Iterable

from .errors import Conflict
from .paths import PORT_RANGE


def is_free(port: int) -> bool:
    """Return True if nothing is listening on ``port`` on either loopback family.

    Both families are checked because macOS resolves ``*.vc.localhost`` to ``::1``
    first; a server bound only to ``127.0.0.1`` still conflicts with one binding
    the wildcard address, and vice versa.

    A bind test cannot be used on a privileged port by an unprivileged caller:
    the kernel answers ``EACCES`` before it ever looks at whether the port is
    taken, which would report every port below 1024 as occupied. In that one case
    the question is answered by trying to connect instead.
    """
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            # Servers set SO_REUSEADDR, so this probe must too, or it answers a
            # different question than the one being asked. Without it a port
            # left in TIME_WAIT by a connection the previous process accepted
            # reads as occupied for a full two minutes on macOS, even though
            # the app being started would bind it without complaint. With it,
            # an actually-listening socket still refuses the bind, which is the
            # case worth reporting.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((addr, port))
            except PermissionError:
                return not is_listening(port)
            except OSError as exc:
                if exc.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
                    return False
                return not is_listening(port)
    return True


def is_listening(port: int, timeout: float = 0.3) -> bool:
    """Return True if a TCP connect to ``port`` on loopback succeeds."""
    for addr in ("127.0.0.1", "::1"):
        with contextlib.suppress(OSError):
            with socket.create_connection((addr, port), timeout=timeout):
                return True
    return False


def find_free(claimed: Iterable[int] = ()) -> int:
    """Return the lowest unclaimed, unbound port in :data:`PORT_RANGE`.

    ``claimed`` holds ports that are spoken for in the registry but whose app is
    not running, which a bind test alone would report as free.

    Raises:
        Conflict: if every port in the range is taken.
    """
    taken = set(claimed)
    low, high = PORT_RANGE
    for port in range(low, high + 1):
        if port not in taken and is_free(port):
            return port
    raise Conflict(
        f"no free port in {low}-{high}",
        hint="stop an app with `vibe-caddy stop <name>`, or free a port by hand",
    )


def describe_holder(port: int) -> str:
    """Best-effort description of what holds ``port``, for error messages.

    Shells out to ``lsof``, which only reports sockets the caller is allowed to
    see: an unprivileged process cannot see a listener opened by Docker's
    privileged helper, and gets an empty answer rather than an error. Returns an
    empty string whenever nothing can be determined, so callers can append it
    without a guard.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "cn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return ""

    commands = sorted({line[1:] for line in out.splitlines() if line.startswith("c")})
    return ", ".join(commands)
