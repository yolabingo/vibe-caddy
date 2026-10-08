"""Framework presets: commands that are known to bind ``$PORT``.

The single most common way this tool appears broken is a start command that
ignores the port it was given. The app binds the framework's own default, vibe
proxies to the port it assigned, and the route answers nothing -- or worse,
answers whatever else later claims that port. Frameworks fall into three groups:

* those that read ``PORT`` from the environment by themselves;
* those that need the port passed as an explicit flag;
* those that additionally re-exec themselves for auto-reload, which hides the
  real process from launchd and has to be turned off.

A preset encodes the right answer for one framework so the user never has to
know which group it is in.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import VibeError


@dataclass(frozen=True, slots=True)
class Framework:
    """One framework preset."""

    name: str
    #: Human-readable label for listings.
    label: str
    #: Shell command template. ``$PORT`` is left literal: launchd supplies it
    #: from the job environment. ``{host}`` and ``{url}`` are filled in with the
    #: route's hostname when the file is written, for the frameworks that have
    #: to be told their own public address.
    cmd: str
    #: Files that must exist for auto-detection to consider this framework.
    detect_files: tuple[str, ...] = ()
    #: ``package.json`` dependency names that identify it.
    detect_packages: tuple[str, ...] = ()
    #: Substrings that must appear in one of ``detect_files`` to confirm.
    detect_contains: tuple[str, ...] = ()
    #: Python distributions in ``pyproject.toml`` that identify it.
    detect_python: tuple[str, ...] = ()
    #: True when the command cannot pass the port and the application is
    #: expected to read ``PORT`` from the environment itself. These are the
    #: presets that can silently bind the wrong port, so they always carry a
    #: note saying so.
    reads_port_env: bool = False
    #: Shown after scaffolding: configuration the user must still apply.
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: Higher wins when several presets match the same project.
    priority: int = 0


def render_cmd(template: str, host: str) -> str:
    """Fill a preset's command template in for one route."""
    return template.format(host=host, url=f"https://{host}")


def _package_json(root: Path) -> dict[str, object]:
    path = root / "package.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def dependencies(root: Path) -> set[str]:
    """Every declared npm dependency name, across all dependency sections."""
    data = _package_json(root)
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            names |= set(section)
    return names


def python_dependencies(root: Path) -> set[str]:
    """Distribution names from ``pyproject.toml``, lowercased.

    Only the names are taken; version specifiers are irrelevant here and parsing
    them properly would mean depending on a requirements parser.
    """
    path = root / "pyproject.toml"
    if not path.is_file():
        return set()
    try:
        data = tomllib.loads(path.read_text())
    except OSError, tomllib.TOMLDecodeError:
        return set()

    found: set[str] = set()
    project = data.get("project", {})
    if isinstance(project, dict):
        for entry in project.get("dependencies", []) or []:
            if isinstance(entry, str):
                found.add(_distribution_name(entry))
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group in optional.values():
                for entry in group or []:
                    if isinstance(entry, str):
                        found.add(_distribution_name(entry))
    return found


def _distribution_name(requirement: str) -> str:
    """Strip extras, markers and version specifiers from a requirement string.

    Whitespace is removed first: a leading space would otherwise make the
    space-separator pass return an empty name for ``"  flask == 1.0"``.
    """
    name = requirement.strip()
    for separator in (";", "[", "=", ">", "<", "!", "~", " "):
        name = name.partition(separator)[0].strip()
    return name.lower()


def get(name: str) -> Framework:
    """Look a preset up by name.

    Raises:
        VibeError: naming the supported frameworks.
    """
    try:
        return REGISTRY[name.lower()]
    except KeyError:
        raise VibeError(
            f"unknown framework {name!r}",
            hint="see the full list with: vibe-caddy init --framework list",
        ) from None


def detect(root: Path) -> Framework | None:
    """Guess the framework used by the project in ``root``.

    Returns the highest-priority match, or None when nothing matches. Priority
    breaks the common ties: a SvelteKit project also depends on Vite, and a
    Django project may also have FastAPI installed for one endpoint.
    """
    npm = dependencies(root)
    python = python_dependencies(root)

    matches = [
        framework for framework in REGISTRY.values() if _matches(framework, root, npm, python)
    ]
    if not matches:
        return None
    return max(matches, key=lambda framework: framework.priority)


def _matches(framework: Framework, root: Path, npm: set[str], python: set[str]) -> bool:
    if framework.detect_packages and npm & set(framework.detect_packages):
        return True
    if framework.detect_python and python & set(framework.detect_python):
        return True
    if not framework.detect_files:
        return False

    present = [name for name in framework.detect_files if (root / name).exists()]
    if not present:
        return False
    if not framework.detect_contains:
        return True
    # A marker file alone is ambiguous -- manage.py is Django's, but a bare
    # app.py says nothing -- so a content check confirms it.
    for name in present:
        try:
            text = (root / name).read_text(errors="replace")
        except OSError:
            continue
        if any(token in text for token in framework.detect_contains):
            return True
    return False


#: Populated below by :data:`FRAMEWORKS`.
REGISTRY: dict[str, Framework] = {}


def _register(*entries: Framework) -> None:
    for entry in entries:
        REGISTRY[entry.name] = entry


# Every JS preset calls the project-local binary rather than going through
# `npm run`: a package-manager wrapper stays alive as the process launchd
# tracks, with the real server as a child it knows nothing about.
_NODE_BIN = "exec ./node_modules/.bin"

_VITE_HOSTS = (
    "Vite blocks unknown Host headers. Add "
    "server: {{ allowedHosts: ['.localhost'] }} to your Vite config."
)

_register(
    # ---------------------------------------------------------------- Python
    Framework(
        name="django",
        label="Django",
        # The autoreloader re-execs into a child, leaving launchd tracking a
        # process that is no longer the server.
        cmd="exec python3 -u manage.py runserver --noreload 127.0.0.1:$PORT",
        detect_files=("manage.py",),
        detect_contains=("DJANGO_SETTINGS_MODULE", "django"),
        detect_python=("django",),
        notes=(
            'Add ALLOWED_HOSTS = [".localhost"] to settings (the leading dot covers subdomains).',
            'Add CSRF_TRUSTED_ORIGINS = ["https://*.localhost"]; the scheme is required.',
        ),
        priority=30,
    ),
    Framework(
        name="flask",
        label="Flask",
        # Flask's own variable is FLASK_RUN_PORT, never PORT, so the port has to
        # be passed explicitly.
        cmd="exec flask run --host 127.0.0.1 --port $PORT --no-reload --no-debugger",
        detect_python=("flask",),
        notes=("Set --app if your application is not in app.py or wsgi.py.",),
        priority=20,
    ),
    Framework(
        name="fastapi",
        label="FastAPI",
        # `fastapi dev` turns reload on by default; under a supervisor that
        # forks a child the parent is no longer the server.
        cmd="exec fastapi dev --no-reload --host 127.0.0.1 --port $PORT",
        detect_python=("fastapi",),
        notes=("Needs the fastapi[standard] extra for the `fastapi` command.",),
        priority=25,
    ),
    Framework(
        name="uvicorn",
        label="Uvicorn (bare ASGI)",
        cmd="exec uvicorn main:app --host 127.0.0.1 --port $PORT",
        detect_python=("uvicorn",),
        notes=("Change main:app if your application object lives elsewhere.",),
        priority=10,
    ),
    Framework(
        name="streamlit",
        label="Streamlit",
        # Without headless mode Streamlit tries to open a browser and prompts
        # for an email on first run, which hangs with no terminal attached.
        cmd=(
            "exec streamlit run app.py --server.port $PORT --server.address 127.0.0.1"
            " --server.headless true --browser.gatherUsageStats false"
        ),
        detect_python=("streamlit",),
        priority=25,
    ),
    Framework(
        name="gradio",
        label="Gradio",
        # Gradio reads GRADIO_SERVER_PORT, not PORT, and by default walks
        # forward through 100 ports if the first is busy -- which would leave it
        # listening somewhere the proxy is not pointing. NUM_PORTS=1 makes that
        # a failure instead of a silent move.
        cmd=(
            "exec env GRADIO_SERVER_NAME=127.0.0.1 GRADIO_SERVER_PORT=$PORT"
            " GRADIO_NUM_PORTS=1 python3 -u app.py"
        ),
        detect_python=("gradio",),
        notes=("Remove any server_port= argument in launch(); it overrides the environment.",),
        priority=25,
    ),
    # ------------------------------------------------------------------ JS/TS
    Framework(
        name="vite",
        label="Vite",
        # --strictPort turns a busy port into a failure rather than a silent
        # move to the next one, which would strand the proxy.
        cmd=f"{_NODE_BIN}/vite --host 127.0.0.1 --port $PORT --strictPort",
        detect_packages=("vite",),
        notes=(_VITE_HOSTS,),
        priority=10,
    ),
    Framework(
        name="next",
        label="Next.js",
        cmd=f"{_NODE_BIN}/next dev --hostname 127.0.0.1 --port $PORT",
        detect_packages=("next",),
        notes=("Add allowedDevOrigins: ['*.localhost'] to next.config for HMR from this host.",),
        priority=40,
    ),
    Framework(
        name="nuxt",
        label="Nuxt",
        # Nuxt forks a worker by default; --no-fork keeps the supervised process
        # the real server. --no-tui stops it drawing a terminal UI into the log.
        cmd=f"{_NODE_BIN}/nuxt dev --host 127.0.0.1 --port $PORT --strictPort --no-fork --no-tui",
        detect_packages=("nuxt",),
        notes=("Add vite: {{ server: {{ allowedHosts: ['.localhost'] }} }} to nuxt.config.",),
        priority=40,
    ),
    Framework(
        name="astro",
        label="Astro",
        # Astro 7 detaches itself when it detects an agent environment and takes
        # a per-project lock; --ignore-lock keeps it in the foreground where
        # launchd can supervise it.
        cmd=f"{_NODE_BIN}/astro dev --ignore-lock --host 127.0.0.1 --port $PORT",
        detect_packages=("astro",),
        notes=(
            "Add server: {{ allowedHosts: ['.localhost'] }} to astro.config.",
            "Astro has no strict-port flag: if the port is taken it moves to another one.",
        ),
        priority=40,
    ),
    Framework(
        name="sveltekit",
        label="SvelteKit",
        # There is no `svelte-kit dev`; SvelteKit is driven by Vite directly.
        cmd=f"{_NODE_BIN}/vite dev --host 127.0.0.1 --port $PORT --strictPort",
        detect_packages=("@sveltejs/kit",),
        notes=(_VITE_HOSTS,),
        priority=40,
    ),
    Framework(
        name="react-router",
        label="React Router (ex-Remix)",
        cmd=f"{_NODE_BIN}/react-router dev --host 127.0.0.1 --port $PORT --strictPort",
        detect_packages=("@react-router/dev", "@remix-run/dev"),
        notes=(_VITE_HOSTS,),
        priority=40,
    ),
    Framework(
        name="angular",
        label="Angular",
        # Angular's own host check is configured on the serve target, not in a
        # Vite config, despite what its error message suggests.
        cmd=f"{_NODE_BIN}/ng serve --host 127.0.0.1 --port $PORT --allowed-hosts {{host}}",
        detect_files=("angular.json",),
        detect_packages=("@angular/cli",),
        priority=40,
    ),
    Framework(
        name="express",
        label="Express / plain Node",
        cmd="exec node server.js",
        detect_packages=("express",),
        reads_port_env=True,
        notes=(
            "Your code must read process.env.PORT; nothing passes it for you.",
            "Do not use nodemon or node --watch: both supervise a child process.",
        ),
        priority=5,
    ),
    # ------------------------------------------------------------------ other
    Framework(
        name="rails",
        label="Ruby on Rails",
        # The pidfile is per-port so two worktrees of one app do not refuse to
        # start over tmp/pids/server.pid.
        cmd="exec bin/rails server -b 127.0.0.1 -p $PORT -P tmp/pids/vibe-$PORT.pid",
        detect_files=("config/application.rb", "Gemfile"),
        detect_contains=("rails",),
        notes=('Add config.hosts << ".localhost" to config/environments/development.rb.',),
        priority=35,
    ),
    Framework(
        name="jekyll",
        label="Jekyll",
        cmd="exec bundle exec jekyll serve --host 127.0.0.1 --port $PORT",
        detect_files=("_config.yml",),
        notes=("Do not add --livereload: its fixed port 35729 collides between sites.",),
        priority=20,
    ),
    Framework(
        name="hugo",
        label="Hugo",
        # Hugo builds absolute URLs from baseURL, and its live-reload client
        # connects to liveReloadPort, so both must describe the public address
        # rather than the internal one.
        cmd=(
            "exec hugo server --bind 127.0.0.1 --port $PORT --baseURL {url}"
            " --appendPort=false --liveReloadPort 443"
        ),
        detect_files=("hugo.toml", "hugo.yaml", "hugo.json", "config.toml"),
        priority=20,
    ),
    Framework(
        name="php",
        label="PHP built-in server",
        cmd="exec php -S 127.0.0.1:$PORT -t public",
        detect_files=("composer.json", "index.php"),
        notes=("Change -t if your document root is not public/.",),
        priority=10,
    ),
    Framework(
        name="go",
        label="Go",
        # `go run` leaves the compiled binary as a child of the go tool, so the
        # build and the run are separated.
        cmd="go build -o .vibe-caddy-bin . && exec ./.vibe-caddy-bin",
        detect_files=("go.mod",),
        reads_port_env=True,
        notes=(
            'Your code must read os.Getenv("PORT"); nothing passes it for you.',
            "Add .vibe-caddy-bin to .gitignore.",
        ),
        priority=10,
    ),
    Framework(
        name="static",
        label="Static files",
        cmd="exec python3 -u -m http.server $PORT --bind 127.0.0.1 --directory .",
        detect_files=("index.html",),
        priority=1,
    ),
)
