# CLAUDE.md

Guidance for working on vibe-caddy itself. `README.md` holds the user-facing docs.

## What this is

vibe-caddy is a stateless Typer CLI (`vibe-caddy`, Python 3.14, uv-only). It maps `https://<name>.localhost` to local dev servers. Three tools do the work:

| Task | Tool |
| --- | --- |
| Name resolution | The built-in `*.localhost` handling of macOS |
| TLS and proxying | Caddy |
| Process supervision | launchd |

There is no vibe daemon.

## Architecture invariant

`$XDG_DATA_HOME/vibe-caddy/registry.json` (default `~/.local/share/vibe-caddy`) is the single source of truth. The registry produces the Caddyfile and the launchd plists. Call them derived files.

| Derived file | Location |
| --- | --- |
| Caddyfile | `$XDG_STATE_HOME/vibe-caddy/Caddyfile` (default `~/.local/state/vibe-caddy`) |
| launchd plists | `$XDG_STATE_HOME/vibe-caddy/launchd/*.plist` |

Nothing reads the derived files back. Never edit them by hand. Never add code that parses them to learn state. To change state, change the registry through `registry.transaction()`. Then regenerate the derived files with `caddy.write`/`caddy.reload` or `launchd.write_plist`.

Every mutation in `service.py` has the same four steps:

1. Take the registry lock.
2. Change the registry.
3. Leave the lock.
4. Reload Caddy with the snapshot.

The CLI and the dashboard both go through `service.py`.

## Module map (`src/vibe_caddy/`)

| Module | Role |
| --- | --- |
| `cli.py` | Typer app with every command, output formatting and error display. No business logic. |
| `service.py` | Route lifecycle (register, update, deregister, start, stop, restart, prune, `start_project`) and `RouteStatus`. The CLI and the dashboard share it. |
| `registry.py` | Loads, saves and locks `registry.json`. Checks port claims. `transaction()` holds an exclusive `flock` for the read-modify-write. |
| `models.py` | Pydantic models: `Route`, `RouteType`, `RegistryData`, `ProjectConfig` (the `vibe-caddy.toml` schema). |
| `project.py` | Finds, parses and scaffolds `vibe-caddy.toml`. Holds `TEMPLATE`. `find_legacy` locates a pre-rename `vibe.toml`, so the error can name it. |
| `frameworks.py` | Preset registry (`REGISTRY`, 20 `Framework` objects), `detect()` and `render_cmd()`. The authority for every preset command and note. |
| `caddy.py` | Renders the Caddyfile. Controls Caddy through the admin API (`is_running`, `is_ours`, `foreign_instance`, `validate`, `reload`). |
| `launchd.py` | Renders app plists. Wraps `launchctl` bootstrap, bootout, kickstart and print. Tails logs. |
| `install.py` | System footprint: root LaunchDaemon, port preflight, Docker hint, CA trust and untrust, root handling. |
| `doctor.py` | Diagnostic checks. Each check returns a `Check` with a fix command. |
| `paths.py` | Every filesystem location and constant (ports, labels, TLD). `paths.home()` is the test seam. `data_dir()` and `state_dir()` resolve the XDG directories. |
| `ports.py` | Free-port probing on both loopback families, `lsof` holder lookup, and auto-assignment in 3000-3999. |
| `names.py` | Route-name validation and slugification. |
| `gitwt.py` | Git worktree detection, and branch and slug derivation. |
| `errors.py` | `VibeError` (with `hint`), `NotFound`, `Conflict`, `SetupRequired`. |
| `dashboard/` | FastAPI app (`app.py`), Jinja templates, static assets. The JSON API is under `/_api`. The middleware holds the CSRF guard. |

## Dev loop

All tooling goes through uv. `uv format` (Ruff formatter) and `uv check` (ty) are uv-native preview commands. `[tool.uv] preview-features` in `pyproject.toml` enables them. Ruff and ty are not dev dependencies. Do not add them. The only dev dependencies are `pytest` and `pytest-cov`.

| Command | What it does |
| --- | --- |
| `just all` | Runs `uv format`, `uv check` and tests. Run it before every commit. |
| `just ci` | Runs `uv format --check`, `uv check`, tests and `uv audit`. CI runs the same steps. |
| `just test [ARGS]` | Runs `uv run pytest [ARGS]`. |
| `just cov` | Runs tests with coverage. |
| `just caddyfile` | Prints the Caddyfile for the current registry. |
| `just validate` | Prints the Caddyfile, then validates it with the real `caddy`. |
| `just dashboard [PORT]` | Runs the dashboard in the foreground (default 7999). |
| `just install-cli` | Runs `uv tool install --force --reinstall .`. |

Python is `>=3.14`. The code uses `except A, B:` without parentheses (PEP 758). The code also uses `from __future__ import annotations`.

## Testing rules

- Isolate `paths.home` to a tmp dir in every test.
  - `tests/conftest.py` does this with an autouse fixture (`isolated_home`).
  - The fixture monkeypatches `paths.home` to `tmp_path`.
  - The fixture removes `SUDO_USER`.
  - The fixture replaces `launchd._run` with a stub that fails.
  - Do not bypass the fixture. Do not construct paths from `Path.home()` in tests.
- Never call the real `launchctl`, `security`, or `caddy reload`/`caddy trust`/`caddy run`.
  - Use the `reloads` fixture to replace `caddy.reload` when the code under test calls it.
  - Add a stub for anything new that shells out to a privileged or stateful tool.
- The Caddy tests (`tests/test_caddy.py`) shell out to the real `caddy` binary for `caddy validate` and `caddy fmt --diff`.
  - The tests skip when `caddy` is not installed.
  - The generator must stay `caddy fmt` clean: tabs for indentation, and exactly the spacing that Caddy produces.
  - If you change `caddy.render`, run `just test tests/test_caddy.py` with Caddy installed.
- Do not touch the real `~/.local/share/vibe-caddy` or `~/.local/state/vibe-caddy`.
  - For manual probing, run `HOME=/tmp/some-dir uv run vibe-caddy ...`.
  - `XDG_DATA_HOME` and `XDG_STATE_HOME` take precedence over `HOME`. Unset them, or point them into the tmp dir.
  - Remember that `service.register` calls `caddy.reload`.
  - That call pushes to a live vibe-caddy Caddy on port 2019 if one runs.

## Conventions

- Set the line length to 100 (`[tool.ruff] line-length`) and the target to `py314`.
- Write Google-style docstrings (`Args:`, `Returns:`, `Raises:`) on public functions that need them.
- Write comments that explain why, not what.
- Raise `VibeError` (or a subclass) with a `hint` for user-facing failures. `cli.fail` prints these errors. Do not let tracebacks reach the user for expected conditions.
- Pass `timeout=` and `check=False` in subprocess calls. Handle the result explicitly.
- Dependencies: `fastapi`, `httpx2`, `jinja2`, `pydantic`, `rich`, `typer`, `uvicorn`. Keep the list short.

## Writing style for agent-facing text

Write all text that an AI agent ingests in ASD-STE100 (Simplified Technical English). This covers `CLAUDE.md`, `AGENTS.md` and any future skill or prompt file in the repo.

Rules:

- Write one idea per sentence.
- Descriptive sentences: 25 words maximum. Procedural sentences: 20 words maximum.
- Use the active voice. Use the present tense for statements that are always true.
- Use one word for one meaning. In this repo the terms are: route, registry, plist, preset, data directory and state directory.
- Write instructions as imperatives.
- Noun clusters: 3 words maximum. Break up longer chains.
- Use a pronoun only when its referent is unambiguous. Otherwise repeat the noun.
- Paragraphs: 6 sentences maximum.
- Keep the articles (a, an, the).
- Quote code, commands, paths, identifiers, file names and error strings exactly.
- Put several related facts in a table or in bullets, not in one long sentence.

Exempt text keeps its own style: `README.md` and other human-facing documentation, commit messages, code comments and docstrings. These texts have different audiences and different conventions. Do not change their style.

Example from the rewrite of this file:

```text
Before: Never hand-edit them and never add code that parses them to learn state; change the
registry (through `registry.transaction()`) and regenerate with `caddy.write`/`caddy.reload`
or `launchd.write_plist`.

After:  Never edit them by hand. Never add code that parses them to learn state. To change
state, change the registry through `registry.transaction()`. Then regenerate the derived
files with `caddy.write`/`caddy.reload` or `launchd.write_plist`.
```

## Filesystem layout

The layout follows XDG and splits by durability.

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

## Non-obvious gotchas

- **`*.localhost` resolves to `::1` first.**
  - Caddy therefore binds both `127.0.0.1` and `::1`. Every block has `bind 127.0.0.1 ::1` (`paths.BIND_HOSTS`).
  - `ports.is_free` and `ports.is_listening` check both families for the same reason.
  - A server bound to only one family can collide with the other family. It can also be unreachable from the other family.
- **Legacy migration.** `setup` runs `install.migrate_legacy()`. The function:
  - moves `registry.json` and `caddy/` into the data directory. The old location is `~/.vibe-caddy`, which `paths.legacy_dir()` returns;
  - skips any target that already holds data;
  - writes `MOVED.txt` in the old directory;
  - does not delete the old directory.
  - It does not migrate derived files, because they embed stale absolute paths. The next command regenerates them.
  - `doctor` warns (`legacy layout`) while the old directory still holds a registry.
- **The `doctor` check `daemon config path`** compares the `--config` path in the installed LaunchDaemon plist with `paths.caddyfile()`.
  - The two paths differ when both of these conditions are true: a custom `XDG_STATE_HOME` is set, and `setup` runs under plain `sudo`. Plain `sudo` resets the environment.
  - The suggested fix is `sudo --preserve-env=XDG_STATE_HOME,XDG_DATA_HOME vibe-caddy setup`.
- **The code pins the Caddy data directory through `XDG_DATA_HOME`** to `<data dir>/caddy` (`paths.caddy_data_dir()`).
  - The pin is in three places: the LaunchDaemon plist, `caddy.caddy_env()` and `install.trust_ca`.
  - The root daemon and the `caddy trust` of the user must share one CA. Otherwise the keychain trusts a root that signs nothing.
  - The code pins `HOME` alongside `XDG_DATA_HOME`.
- **`paths.home()` follows `SUDO_USER`** when the process is root. `setup` runs under sudo, but it must write into the data and state directories of the invoking user. `install.chown_to_user` hands both directories back to that user afterwards.
- **App plists live in `<state dir>/launchd`, not `~/Library/LaunchAgents`.** Registering an app therefore does not start the app at login. Only routes with `autostart = true` get a symlink into LaunchAgents. `launchd.write_plist` creates and removes that symlink.
- **`caddy.is_ours()` exists for one reason.** vibe-caddy must never reconfigure another user's Caddy.
  - It looks for the loopback `ask` listener (`paths.CADDY_ASK_PORT`, 2021) in the running config. Every config that vibe-caddy generates has this listener.
  - `reload` refuses when another process holds the admin port. With `require_running=False`, `reload` skips silently instead.
  - Do not remove the `ask` listener from the generated config without a replacement signature.
- **Each route lists both `https://host` and `http://host`.** Caddy therefore proxies plain HTTP and does not redirect it.
- **Catch-all plus on-demand TLS.** Unregistered names fall through to a hostname-less block. That block proxies to the dashboard (route name `vibe`, `paths.DASHBOARD_ROUTE`). It returns a 404 string when the dashboard is not registered. The `ask` endpoint gates certificate issuance for arbitrary names. The endpoint admits only `*.localhost`.
- **`ws_origin_rewrite`** rewrites `Origin` and `Host` only on WebSocket upgrade requests (`@upgrade` matcher). Next.js HMR needs it.
- **Launchd job shape:**
  - Command: `<login shell> -lc <cmd>`. This form lets version-manager shims resolve.
  - Shell: `paths.user_shell()`. It returns the login shell of the invoking user from the password database. It does not use `$SHELL`.
  - Reason: under `sudo`, `$SHELL` is the `/bin/sh` of root. That shell does not load the user's profile (nvm, pyenv, rbenv).
  - Restart policy: `KeepAlive: {SuccessfulExit: false}` with a 10 s throttle.
  - Environment: `PORT`, `PORT_<KEY>`, `VIBE_ROUTE`, `VIBE_URL`, `VIBE_HOSTNAME`.
- **Worktree routes** follow three rules:
  - They take the app name from the `vibe-caddy.toml` of the main checkout.
  - They never inherit a pinned `port`.
  - Prune removes them only when the parent directory of the checkout still exists. An unmounted volume must not wipe the registry.
- **On re-`start`, only `cmd`, `dir`, `icon`, `autostart`, `ws_origin_rewrite` refresh** from `vibe-caddy.toml`. The route keeps `port` and `reserve_ports` from the first registration.
- **`restart` also re-reads `vibe-caddy.toml`** (`service._refresh_from_project`).
  - It reads the file in the `dir` of the route. It refreshes `cmd`, `icon`, `autostart` and `ws_origin_rewrite`, but never `port`.
  - `restart` uses the stored values in three cases: the route has no `dir`, the route has no file, or the file fails `project.load`. Thus a half-edited file cannot block a restart.
  - Earlier docs claimed that `restart` picked up edits. It did not.
- **No `vibe.toml` fallback.** The file now has the name `vibe-caddy.toml` (`project.CONFIG_NAME`). `start_project` raises `NotFound` only when it finds no `vibe-caddy.toml`. The error names a leftover `vibe.toml` and gives the exact `mv` command. Honouring both names would leave two files that can disagree.
- **Framework presets exist because a command that ignores `$PORT` looks like a working app.** The app binds the framework default port. Caddy proxies to the assigned port. The route answers nothing. These rules apply in `frameworks.py`:
  - Commands start with `exec`. They call project-local binaries (`./node_modules/.bin/vite`, `bin/rails`), not `npm run` or `bundle exec`. A wrapper stays the PID that launchd tracks, and the real server becomes an untracked child.
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
- **Dashboard CSRF guard** (`dashboard/app.py`). The API is unauthenticated. Route creation runs shell commands. The guard refuses non-safe methods on `Sec-Fetch-Site: cross-site` or an untrusted `Origin`. The guard excludes bookmark hostnames from the trusted origins on purpose. The guard allows a missing `Origin` (CLI, curl). Keep that behavior when you add endpoints.
- **`deregister --all` spares the dashboard.** `service.removable()` filters out `paths.DASHBOARD_ROUTE` unless the caller asks for it. It also orders worktree routes before their parent. A parent is thus never dropped while a child still points at it.
- **Bulk removal goes through `service.deregister_many()`.** That function edits the registry once and reloads Caddy once. Looping `deregister()` publishes every intermediate state and does N times the work.
- **Names:** lowercase DNS labels with at most one dot (`<worktree>.<app>`). Reserved names: `local` and `localhost`.

## Known inconsistencies in the code

- `pyproject.toml` sets `readme = "README.md"`. Thus `README.md` is the package description. Keep `README.md` accurate.
