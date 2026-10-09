"""Re-resolve runtime and development dependencies with uv's default version bounds."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def packages(requirements: list[str]) -> tuple[list[str], list[str]]:
    """Strip version bounds while preserving extras and environment markers."""
    names = []
    requests = []
    for requirement in requirements:
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)(\s*\[[^]]+\])?", requirement)
        if match is None or "@" in requirement.split(";", 1)[0]:
            raise ValueError(f"cannot bump non-index dependency: {requirement}")
        names.append(match[1])
        request = match[0].strip()
        if ";" in requirement:
            request += ";" + requirement.split(";", 1)[1]
        requests.append(request)
    return list(dict.fromkeys(names)), requests


def run(*args: str) -> None:
    print("uv " + " ".join(args), flush=True)
    result = subprocess.run(["uv", *args], cwd=ROOT, timeout=300, check=False)
    if result.returncode:
        raise RuntimeError(f"uv {args[0]} failed with exit code {result.returncode}")


def main(exclude_newer: str) -> int:
    project = ROOT / "pyproject.toml"
    lock = ROOT / "uv.lock"
    original = project.read_bytes()
    original_lock = lock.read_bytes() if lock.exists() else None
    try:
        config = tomllib.loads(original.decode())
        batches = [((), packages(config["project"].get("dependencies", [])))]
        for group, entries in config.get("dependency-groups", {}).items():
            flags = ("--dev",) if group == "dev" else ("--group", group)
            batches.append(
                (flags, packages([entry for entry in entries if isinstance(entry, str)]))
            )
        for extra, entries in config["project"].get("optional-dependencies", {}).items():
            batches.append((("--optional", extra), packages(entries)))

        # Remove every old direct constraint before resolving any new version.
        # Delay syncing so the environment stays usable until resolution succeeds.
        for flags, (names, _) in batches:
            if names:
                run("remove", "--no-sync", *flags, *names)
        for flags, (_, requests) in batches:
            if requests:
                run("add", "--no-sync", "--exclude-newer", exclude_newer, *flags, *requests)
        run("sync")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        project.write_bytes(original)
        if original_lock is not None:
            lock.write_bytes(original_lock)
        else:
            lock.unlink(missing_ok=True)
        print(
            f"Dependency bump failed: {exc}. Restored dependency files. Run uv sync.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exclude-newer", required=True, help="Latest allowed package release date"
    )
    raise SystemExit(main(parser.parse_args().exclude_newer))
