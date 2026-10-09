# CLAUDE.md

Use this file when you work on vibe-caddy. `README.md` contains the documentation for users.

## What this is

vibe-caddy is a stateless Typer CLI. The CLI uses Python 3.14 and uv.
The CLI maps `https://<name>.vc.localhost` to local development servers.
The dashboard uses `https://vibe.vc.localhost`.
Three tools provide the system functions:

| Task | Tool |
| --- | --- |
| Name resolution | macOS resolves names under `.localhost` to loopback. This includes `.vc.localhost`. |
| TLS and proxying | Caddy |
| Process supervision | launchd |

There is no vibe daemon.

## Architecture invariant

The registry is the single source of truth.
The registry file is `$XDG_DATA_HOME/vibe-caddy/registry.json`.
The default location is `~/.local/share/vibe-caddy/registry.json`.
The registry produces the Caddyfile and the plists. These files are derived files.

| Derived file | Location |
| --- | --- |
| Caddyfile | `$XDG_STATE_HOME/vibe-caddy/Caddyfile` (default `~/.local/state/vibe-caddy`) |
| launchd plists | `$XDG_STATE_HOME/vibe-caddy/launchd/*.plist` |

Never edit the derived files by hand.
Never parse the derived files to obtain route state.
Change the registry through `registry.transaction()`.
Regenerate the derived files with `caddy.write`, `caddy.reload`, or `launchd.write_plist`.

Use this sequence for registry mutations in `service.py`:

1. Take the registry lock.
2. Change the registry.
3. Release the registry lock.
4. Call `caddy.reload`.

The CLI and the dashboard both go through `service.py`.

`caddy.reload` reads the current registry under a separate publication lock.
This lock prevents an older configuration from replacing a newer configuration.
Do not pass a registry snapshot to `caddy.reload`.

## Module map (`src/vibe_caddy/`)

| Module | Role |
| --- | --- |
| `cli.py` | Defines the Typer commands. Formats output and errors. Contains no business logic. |
| `service.py` | Route lifecycle (register, update, deregister, start, stop, restart, prune, `start_project`) and `RouteStatus`. The CLI and the dashboard share it. |
| `registry.py` | Loads, saves and locks `registry.json`. Checks port claims. `transaction()` holds an exclusive `flock` for the read-modify-write. |
| `models.py` | Pydantic models: `Route`, `RouteType`, `RegistryData`, `ProjectConfig` (the `vibe-caddy.toml` schema). |
| `project.py` | Finds, parses and scaffolds `vibe-caddy.toml`. Holds `TEMPLATE`. `find_legacy` locates a pre-rename `vibe.toml`, so the error can name it. |
| `frameworks.py` | Preset registry (`REGISTRY`, 20 `Framework` objects), `detect()` and `render_cmd()`. The authority for every preset command and note. |
| `caddy.py` | Renders the Caddyfile. Controls Caddy through the admin API (`is_running`, `is_ours`, `foreign_instance`, `validate`, `reload`). |
| `launchd.py` | Renders app plists. Wraps `launchctl` bootstrap, bootout, kickstart and print. Tails logs. |
| `install.py` | Installs the root daemon. Checks ports. Controls CA trust and ownership. |
| `provision.py` | Controls setup, removal, and installation of the dashboard. |
| `doctor.py` | Diagnostic checks. Each check returns a `Check` with a fix command. |
| `paths.py` | Defines paths and constants. `paths.home()` supplies the test seam. `paths.TLD` contains the hostname suffix `vc.localhost`. |
| `ports.py` | Free-port probing on both loopback families, `lsof` holder lookup, and auto-assignment in 3000-3999. |
| `names.py` | Validates route names. Converts text to route names. |
| `gitwt.py` | Git worktree detection, and branch and slug derivation. |
| `errors.py` | `VibeError` (with `hint`), `NotFound`, `Conflict`, `SetupRequired`. |
| `dashboard/` | FastAPI app (`app.py`), Jinja templates, static assets. The JSON API is under `/_api`. The middleware holds the CSRF guard. |

## Dev loop

Use uv for all tools.
`uv format` uses Ruff. `uv check` uses ty.
These commands use preview features from `[tool.uv]` in `pyproject.toml`.
Do not add Ruff or ty as dependencies.
The development dependencies are `prek`, `pytest`, and `pytest-cov`.

Runtime and development dependencies use uv's default lower bounds.
Use `just deps-bump` to refresh these requirements.
The command removes the current requirements and adds the package names with `uv add`.
Do not supply versions or bounds when you refresh the requirements.
The Just recipe uses Perl to calculate the date five days ago.
The command passes that date to `uv add --exclude-newer`.

| Command | What it does |
| --- | --- |
| `just all` | Runs `uv format`, `uv check` and tests. Run it before every commit. |
| `just ci` | Runs `uv format --check`, `uv check`, tests and `uv audit`. CI runs the same steps. |
| `just deps-bump` | Reads the dependencies. Removes the requirements. Uses normal `uv add` behavior. Updates the lock and environment. |
| `just install` | Installs dependencies and the prek Git hooks. |
| `just hooks-install` | Installs the prek Git hooks. |
| `just hooks` | Runs all prek hooks on all files. |
| `just test [ARGS]` | Runs `uv run pytest [ARGS]`. |
| `just cov` | Runs tests with coverage. |
| `just caddyfile` | Prints the Caddyfile for the current registry. |
| `just validate` | Prints the Caddyfile, then validates it with the real `caddy`. |
| `just dashboard [PORT]` | Runs the dashboard in the foreground (default 7999). |
| `just install-cli` | Installs the CLI. Prints setup, reload, dashboard, and app restart commands. |

Python is `>=3.14`. The code uses `except A, B:` without parentheses (PEP 758). The code also uses `from __future__ import annotations`.

The prek hooks use `.pre-commit-config.yaml`.
The hooks check merge conflicts, YAML, TOML, formatting, types, and tests.
The file checks use the built-in tools from the prek version in `uv.lock`.
The Python checks use uv's managed tools and the project dependencies.

Dependabot checks the uv dependencies and GitHub Actions each week.
The uv updates use a five-day delay after each release.
CI runs the hooks on macOS for each pull request and push to `main`.
CI runs `uv audit --locked` each day and for each pull request and push to `main`.

## Testing rules

- Isolate `paths.home` in a temporary directory in every test.
  - `tests/conftest.py` does this with an autouse fixture (`isolated_home`).
  - The fixture monkeypatches `paths.home` to `tmp_path`.
  - The fixture removes `SUDO_USER`.
  - The fixture replaces `launchd._run` with a stub that fails.
  - Do not bypass the fixture. Do not construct paths from `Path.home()` in tests.
- Never call the real `launchctl`, `security`, or `caddy reload`/`caddy trust`/`caddy run`.
  - Use the `reloads` fixture to replace `caddy.reload` when the code under test calls it.
  - Add a stub for each new subprocess call that uses privileges or changes system state.
- The Caddy tests (`tests/test_caddy.py`) shell out to the real `caddy` binary for `caddy validate` and `caddy fmt --diff`.
  - The tests skip when `caddy` is not installed.
  - The generated Caddyfile must match `caddy fmt` output.
  - Use tabs for indentation. Use the spacing that Caddy produces.
  - Use `caddy adapt` to check certificate policies without starting Caddy.
  - If you change `caddy.render`, run `just test tests/test_caddy.py` with Caddy installed.
- Do not touch the real `~/.local/share/vibe-caddy` or `~/.local/state/vibe-caddy`.
  - For manual probing, run `HOME=/tmp/some-dir uv run vibe-caddy ...`.
  - `XDG_DATA_HOME` and `XDG_STATE_HOME` take precedence over `HOME`.
  - Clear both variables, or set both variables to temporary directories.
  - `service.register` calls `caddy.reload`.
  - That call pushes to a live vibe-caddy Caddy on port 2019 if one runs.

## Conventions

- Set the line length to 100 (`[tool.ruff] line-length`) and the target to `py314`.
- Write Google-style docstrings (`Args:`, `Returns:`, `Raises:`) on public functions that need them.
- Write comments that explain why, not what.
- Raise `VibeError` or a subclass for expected errors.
- Supply a `hint` for errors that users can repair.
- Use `cli.fail` to display expected errors.
- Do not show tracebacks for expected errors.
- Pass `timeout=` and `check=False` in subprocess calls. Handle the result explicitly.
- Dependencies: `fastapi`, `httpx2`, `jinja2`, `pydantic`, `rich`, `typer`, `uvicorn`. Keep the list short.

## Writing style for agent-facing text

Use ASD-STE100 Simplified Technical English for text that AI agents read.
Apply these rules to `CLAUDE.md`, `AGENTS.md`, and future skills or prompts in this repository.

Rules:

- Write one idea per sentence.
- Descriptive sentences: 25 words maximum. Procedural sentences: 20 words maximum.
- Use the active voice. Use the present tense for statements that are always true.
- Use one word for one meaning.
- Use the terms route, registry, plist, preset, data directory, and state directory consistently.
- Write instructions as imperatives.
- Noun clusters: 3 words maximum. Break up longer chains.
- Use a pronoun only when its referent is unambiguous. Otherwise repeat the noun.
- Paragraphs: 6 sentences maximum.
- Keep the articles (a, an, the).
- Quote code, commands, paths, identifiers, file names and error strings exactly.
- Put several related facts in a table or in bullets, not in one long sentence.

`README.md` and other documentation for users retain their own style.
Commit messages, code comments, and docstrings retain their own style.
Do not apply these text rules to those documents.

## Filesystem layout

The layout follows XDG. The directories separate durable data from derived files.

| Directory | Resolves from | Default | Holds |
| --- | --- | --- | --- |
| `paths.data_dir()` | `$XDG_DATA_HOME/vibe-caddy` | `~/.local/share/vibe-caddy` | What cannot regenerate: `registry.json` and `caddy/` (Caddy's local CA and keys) |
| `paths.state_dir()` | `$XDG_STATE_HOME/vibe-caddy` | `~/.local/state/vibe-caddy` | Everything derived: `Caddyfile`, `registry.lock`, `launchd/*.plist`, `log/*.log` |

- State is safe to delete. The next command rebuilds it.
- The code ignores a relative `XDG_*` value, as the spec requires.
- The fallback uses `paths.home()`. Under `sudo`, it resolves to the home of the invoking user.
- The code uses XDG, not `~/Library/Application Support`, for these reasons:
  - XDG is the convention for a standalone CLI on macOS.
  - Apple's location is for GUI apps and their companion CLIs.
  - A user cannot redirect Apple's location. A user can redirect the XDG variables.
  - `gh`, `uv`, `gcloud` and `helm` do the same.
- Two paths stay outside XDG, because launchd dictates them:
  - `/Library/LaunchDaemons/dev.vibe-caddy.caddy.plist` (root daemon).
  - `~/Library/LaunchAgents/dev.vibe-caddy.<name>.plist` (the opt-in `autostart` symlink).

## Operational rules

### Hostname suffix

- Use `paths.hostname` and `paths.url` to construct route addresses.
- Serve routes only below `.vc.localhost`.
- A worktree uses `<worktree>.<app>.vc.localhost`.
- The bare names `localhost` and `vc.localhost` are not route addresses.
- Keep `localhost` as a loopback hostname for direct connections to a server.
- The registry stores route names. The registry does not store generated route hostnames.
- A suffix change therefore requires no registry migration.
- Reload Caddy after a suffix change.
- Restart each managed route to regenerate its plist and environment.
- Update project commands that contain a hostname.
- Update framework settings for allowed hosts and trusted origins.
- Caddy issues separate certificates for routes and worktrees from the local CA.
- A certificate for `*.vc.localhost` does not cover `<worktree>.<app>.vc.localhost`.
- The existing trusted CA remains valid after a suffix change.
- Do not delete the CA to change the hostname suffix.

### System behavior

- **`*.vc.localhost` resolves to `::1` first.**
  - Caddy therefore binds both `127.0.0.1` and `::1`. Every block has `bind 127.0.0.1 ::1` (`paths.BIND_HOSTS`).
  - `ports.is_free` and `ports.is_listening` check both families for the same reason.
  - A server bound to only one family can collide with the other family. It can also be unreachable from the other family.
- **Legacy migration.** `setup` runs `install.migrate_legacy()`.
  - The function moves `registry.json` and `caddy/` into the data directory.
  - `paths.legacy_dir()` returns the old location, `~/.vibe-caddy`.
  - The function skips a destination that already contains data.
  - The function writes `MOVED.txt` in the old directory.
  - The function keeps the old directory.
  - The function does not move derived files because those files contain old absolute paths.
  - The next command regenerates derived files.
  - `doctor` warns (`legacy layout`) while the old directory still holds a registry.
- **The `doctor` check `daemon config path`** compares the `--config` path in the installed LaunchDaemon plist with `paths.caddyfile()`.
  - Plain `sudo` clears custom XDG variables and can cause these paths to differ.
  - The suggested fix is `sudo --preserve-env=XDG_STATE_HOME,XDG_DATA_HOME vibe-caddy setup`.
- **The code sets `XDG_DATA_HOME`** to `paths.caddy_data_dir()` for Caddy.
  - The daemon plist, `caddy.caddy_env()`, and `install.trust_ca` use the same value.
  - The root daemon and `caddy trust` must use the same CA.
  - The code also sets `HOME` to `paths.home()`.
- **`paths.home()` follows `SUDO_USER`** when the process runs as root.
  - `setup` writes into the invoking user's data directory and state directory.
  - `install.chown_to_user` returns both directories to the invoking user after setup.
- **App plists reside in `<state dir>/launchd`.**
  - Registration does not start an app at login.
  - Only `autostart = true` creates a symlink in `~/Library/LaunchAgents`.
  - `launchd.write_plist` creates or removes that symlink.
- **`caddy.is_ours()` identifies the Caddy instance.**
  - The function checks for the loopback `ask` listener on `paths.CADDY_ASK_PORT`, port 2021.
  - Every generated configuration includes this listener.
  - `caddy.reload` refuses to change another Caddy instance.
  - With `require_running=False`, the reload skips that instance without an error.
  - Do not remove the listener without another method to identify the instance.
- **Each route lists both `https://host` and `http://host`.** Caddy therefore proxies plain HTTP and does not redirect it.
- **Fallback and on-demand TLS.**
  - Unregistered names below `.vc.localhost` reach the dashboard through the block without a hostname.
  - The dashboard route name is `vibe`, from `paths.DASHBOARD_ROUTE`.
  - The block returns a 404 string when no dashboard is registered.
  - The block returns a 404 for hostnames outside `.vc.localhost`.
  - The `ask` endpoint admits certificate requests only for names below `.vc.localhost`.
- **`ws_origin_rewrite`** rewrites `Origin` and `Host` only on WebSocket upgrade requests (`@upgrade` matcher). Next.js HMR needs it.
- **Launchd job shape:**
  - Command: `<login shell> -lc <cmd>`. This form lets version-manager shims resolve.
  - Shell: `paths.user_shell()`. It returns the login shell of the invoking user from the password database. It does not use `$SHELL`.
  - Reason: under `sudo`, `$SHELL` is the `/bin/sh` of root. That shell does not load the user's profile (nvm, pyenv, rbenv).
  - Restart policy: `KeepAlive: {SuccessfulExit: false}` with a 10 s throttle.
  - Environment: `PORT`, `PORT_<KEY>`, `VIBE_ROUTE`, `VIBE_URL`, `VIBE_HOSTNAME`.
- **Worktree routes** follow three rules:
  - Worktree routes use the app name from the main checkout's `vibe-caddy.toml`.
  - Worktree routes do not inherit a fixed port.
  - Prune removes a missing worktree only when the checkout's parent directory exists.
  - This rule preserves routes on an unmounted volume.
- **Repeated `start` refreshes the route.**
  - The command reads `cmd`, `dir`, `icon`, `autostart`, and `ws_origin_rewrite` from `vibe-caddy.toml`.
  - The route retains its original `port` and `reserve_ports`.
- **`restart` also re-reads `vibe-caddy.toml`** (`service._refresh_from_project`).
  - The function reads `vibe-caddy.toml` in the route's directory.
  - The function refreshes `cmd`, `icon`, `autostart`, and `ws_origin_rewrite`.
  - The function keeps the port.
  - `restart` uses stored values when the directory or file is absent, or when `project.load` fails.
  - A file with incomplete edits therefore cannot block a restart.
  - Earlier docs claimed that `restart` picked up edits. It did not.
- **The project file is `vibe-caddy.toml`.**
  - `project.CONFIG_NAME` contains this file name.
  - Do not add a fallback to `vibe.toml`.
  - `start_project` raises `NotFound` when no `vibe-caddy.toml` exists.
  - The error identifies a remaining `vibe.toml` and supplies the exact `mv` command.
  - Two accepted file names could produce conflicting configurations.
- **Each framework preset must bind `$PORT`.**
  - An app can start on the framework's default port while Caddy proxies to a different port.
  - This condition leaves the route unreachable.
  - Start preset commands with `exec`.
  - Use project binaries such as `./node_modules/.bin/vite` and `bin/rails`.
  - Do not use wrappers such as `npm run` or `bundle exec` in presets.
  - launchd must track the actual server process.
  - Disable re-exec auto-reloaders (Django, Flask, FastAPI `dev`, Nuxt's fork) for the same reason.
  - Bind servers to `127.0.0.1` explicitly. `localhost` can resolve to `::1` while Caddy dials `127.0.0.1`.
  - `express` and `go` cannot pass the port. They set `reads_port_env` and carry a note.
  - `render_cmd` fills `{host}` and `{url}` in a command template. `{{ }}` in notes are literal braces.
- **`init` and presets:**
  - Plain `init` runs `frameworks.detect` and writes the file without starting the app.
  - `-w NAME|auto` writes the file and then calls `service.start_project`.
  - `-w list` exits before it touches the directory.
  - An explicit `--cmd` beats the command of the preset.
  - `init` never writes the port to the file.
  - Detection ties break on `Framework.priority` (Next and SvelteKit over Vite, FastAPI over uvicorn).
  - Content checks (`detect_contains`) confirm ambiguous marker files such as `manage.py`.
- **Dashboard CSRF guard** (`dashboard/app.py`).
  - The API has no authentication.
  - Route creation runs shell commands.
  - The guard rejects unsafe methods with `Sec-Fetch-Site: cross-site` or an untrusted `Origin`.
  - The guard excludes bookmark hostnames from trusted origins.
  - The guard permits requests without `Origin`, such as CLI and curl requests.
  - Keep this behavior when you add endpoints.
- **`deregister --all` preserves the dashboard.**
  - `service.removable()` filters `paths.DASHBOARD_ROUTE` unless the caller requests its removal.
  - The function orders worktrees before their parent.
  - This order prevents removal of a parent while a child still refers to that parent.
- **Use `service.deregister_many()` for bulk removal.**
  - The function changes the registry once and reloads Caddy once.
  - Do not loop over `deregister()` for bulk removal.
- **Names:** lowercase DNS labels with at most one dot (`<worktree>.<app>`). Reserved names: `local` and `localhost`.

## Known inconsistencies in the code

- `pyproject.toml` sets `readme = "README.md"`. Thus `README.md` is the package description. Keep `README.md` accurate.
