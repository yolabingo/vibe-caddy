"""vibe.toml discovery, parsing and scaffolding."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_caddy import project
from vibe_caddy.errors import VibeError

FULL = """\
name = "web"
cmd = "npm run dev"
port = 3100
icon = "zap"
autostart = true
ws_origin_rewrite = false

[reserve_ports]
websocket = 3101
other = 3102
"""


def test_find_walks_up_from_subdirectory(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    deep = root / "a" / "b"
    deep.mkdir(parents=True)
    (root / "vibe-caddy.toml").write_text(FULL)
    assert project.find(deep) == (root / "vibe-caddy.toml").resolve()


def test_find_returns_none_when_absent(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert project.find(tmp_path / "empty") is None


def test_load_parses_full_config(tmp_path: Path) -> None:
    path = tmp_path / "vibe-caddy.toml"
    path.write_text(FULL)
    config = project.load(path)
    assert (config.name, config.cmd, config.port, config.icon) == (
        "web",
        "npm run dev",
        3100,
        "zap",
    )
    assert config.autostart is True
    assert config.ws_origin_rewrite is False
    assert config.reserve_ports == {"websocket": 3101, "other": 3102}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("name = ", "cannot read"),
        ('name = "x"\ncmd = "y"\nbogus = 1\n', "not a valid vibe config"),
        ('name = "x"\n', "not a valid vibe config"),
        ('name = "x"\ncmd = "y"\nport = 99999\n', "not a valid vibe config"),
    ],
)
def test_load_rejects_bad_files(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "vibe-caddy.toml"
    path.write_text(content)
    with pytest.raises(VibeError, match=message):
        project.load(path)


def test_load_missing_file_raises_vibe_error(tmp_path: Path) -> None:
    with pytest.raises(VibeError, match="cannot read"):
        project.load(tmp_path / "nope.toml")


def test_scaffold_round_trips_through_load(tmp_path: Path) -> None:
    path = project.scaffold(tmp_path, "my-app", "uv run app --port $PORT")
    config = project.load(path)
    assert config.name == "my-app"
    assert config.cmd == "uv run app --port $PORT"
    assert config.port is None


def test_reserve_port_zero_means_auto(tmp_path: Path) -> None:
    path = tmp_path / "vibe-caddy.toml"
    path.write_text('name = "x"\ncmd = "y"\n[reserve_ports]\nwebsocket = 0\n')
    assert project.load(path).reserve_ports == {"websocket": 0}


def test_scaffold_refuses_to_overwrite(tmp_path: Path) -> None:
    project.scaffold(tmp_path, "a", "x")
    with pytest.raises(VibeError, match="already exists"):
        project.scaffold(tmp_path, "b", "y")
    assert 'name = "a"' in (tmp_path / "vibe-caddy.toml").read_text()


AWKWARD_COMMANDS = [
    'exec python3 -c "print(1)"',
    "echo 'it works'",
    """echo "it's" 'both'""",
    r"echo a\nb",
    r"C:\Users\dev\app.exe --port $PORT",
    "node server.js --port $PORT",
    "run # not a comment",
    "line one\nline two\n\ttabbed",
    "ctrl \x01 \x7f \r end",
    'trailing quote"',
    "trailing backslash\\",
    "unicode \u00e9 \u2603",
]


@pytest.mark.parametrize("cmd", AWKWARD_COMMANDS)
def test_scaffold_round_trips_awkward_commands(tmp_path: Path, cmd: str) -> None:
    """Pins finding 9: scaffolded TOML must load back to exactly the input."""
    path = project.scaffold(tmp_path, "my-app", cmd)
    assert project.load(path).cmd == cmd


@pytest.mark.parametrize("cmd", AWKWARD_COMMANDS)
def test_scaffold_round_trips_with_framework(tmp_path: Path, cmd: str) -> None:
    path = project.scaffold(tmp_path, "my-app", cmd, framework="vite")
    config = project.load(path)
    assert (config.name, config.cmd) == ("my-app", cmd)


def test_scaffold_escapes_name_and_icon_independently(tmp_path: Path) -> None:
    # An icon is commented out, so the risk is a newline ending the comment and
    # leaving bare text in the file.
    path = project.scaffold(tmp_path, "my-app", "x", icon='a"\nb = 1')
    assert project.load(path).name == "my-app"


def test_every_framework_preset_round_trips(tmp_path: Path) -> None:
    from vibe_caddy import frameworks

    assert frameworks.REGISTRY
    for name, framework in frameworks.REGISTRY.items():
        target = tmp_path / name
        target.mkdir()
        path = project.scaffold(target, "my-app", framework.cmd, framework=name)
        assert project.load(path).cmd == framework.cmd, name
