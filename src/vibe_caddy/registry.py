"""The route registry: ``$XDG_DATA_HOME/vibe-caddy/registry.json``.

This file is the single source of truth. The Caddyfile and every launchd plist are
derived artifacts, regenerated from it; nothing reads them back. Mutations go
through :func:`transaction`, which holds an exclusive lock for the whole
read-modify-write so two concurrent ``vibe-caddy`` invocations cannot clobber each other.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from . import paths
from .errors import Conflict, NotFound
from .models import RegistryData, Route


def _lock_file() -> Path:
    return paths.state_dir() / "registry.lock"


def load() -> RegistryData:
    """Read the registry, returning an empty one if it does not exist yet."""
    path = paths.registry_file()
    if not path.exists():
        return RegistryData()
    try:
        return RegistryData.model_validate_json(path.read_text())
    except ValueError as exc:
        raise Conflict(
            f"{path} is not valid: {exc}",
            hint=f"move it aside and re-register, or repair it by hand: {path}",
        ) from exc


def save(data: RegistryData) -> None:
    """Write the registry atomically.

    A partially written registry would be unparseable on the next invocation, so
    the new contents land in a sibling temp file and are renamed over the target.
    """
    paths.ensure_dirs()
    target = paths.registry_file()
    payload = data.model_dump_json(indent=2, exclude_none=False) + "\n"

    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".registry-", suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o644)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def transaction() -> Generator[RegistryData]:
    """Yield the registry under an exclusive lock, saving it on clean exit.

    The lock is advisory and process-wide, which is enough: only ``vibe-caddy`` and the
    dashboard (itself a ``vibe-caddy`` process) ever write this file.
    """
    paths.ensure_dirs()
    lock = _lock_file()
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            data = load()
            yield data
            save(data)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def get(name: str) -> Route:
    """Return one route.

    Raises:
        NotFound: if no route by that name is registered.
    """
    data = load()
    try:
        return data.routes[name]
    except KeyError:
        known = ", ".join(sorted(data.routes)) or "none"
        raise NotFound(f"no route named {name!r}", hint=f"registered: {known}") from None


def claimed_ports(data: RegistryData, *, excluding: str | None = None) -> set[int]:
    """Every port claimed by the registry, optionally ignoring one route.

    ``excluding`` is the route being updated: its own current ports must not count
    as a conflict with itself.
    """
    claimed: set[int] = set()
    for name, route in data.routes.items():
        if name != excluding:
            claimed |= route.all_ports()
    return claimed


def assert_port_available(data: RegistryData, port: int, *, excluding: str | None = None) -> None:
    """Raise if ``port`` is already spoken for by another route.

    Raises:
        Conflict: naming the route that holds the port.
    """
    for name, route in data.routes.items():
        if name != excluding and port in route.all_ports():
            raise Conflict(
                f"port {port} is already claimed by route {name!r}",
                hint="pick another port, or omit it to have one assigned",
            )
