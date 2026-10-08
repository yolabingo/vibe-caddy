# CLAUDE.md

Guidance for working on vibe-caddy itself. User-facing docs are in `README.md`.

## What this is

A stateless Typer CLI (`vibe-caddy`, Python 3.14, uv-only) that maps `https://<name>.localhost` to local dev servers. Name resolution is macOS's built-in `*.localhost` handling, TLS and proxying are Caddy, and process supervision is launchd. There is no vibe daemon.

## Architecture invariant

`$XDG_DATA_HOME/vibe-caddy/registry.json` (default `~/.local/share/vibe-caddy`) is the single source of truth. The Caddyfile (`$XDG_STATE_HOME/vibe-caddy/Caddyfile`, default `~/.local/state/vibe-caddy`) and the launchd plists (`$XDG_STATE_HOME/vibe-caddy/launchd/*.plist`) are derived artifacts. Nothing reads them back. Never hand-edit them and never add code that parses them to learn state; change the registry (through `registry.transaction()`) and regenerate with `caddy.write`/`caddy.reload` or `launchd.write_plist`.

Every mutation in `service.py` has the same shape: take the registry lock, change the registry, leave the lock, then reload Caddy with the snapshot. The CLI and the dashboard both go through `service.py`.

## Module map (`src/vibe_caddy/`)

| Module | Role |
| --- | --- |
| `cli.py` | Typer app: every command, output formatting, error display. No business logic. |
| `service.py` | Route lifecycle (register, update, deregister, start, stop, restart, prune, `start_project`) and `RouteStatus`. Shared by CLI and dashboard. |
| `registry.py` | Load/save/lock `registry.json`; port-claim checks. `transaction()` holds an exclusive `flock` for the read-modify-write. |
| `models.py` | Pydantic models: `Route`, `RouteType`, `RegistryData`, `ProjectConfig` (the `vibe-caddy.toml` schema). |
| `project.py` | Find, parse and scaffold `vibe-caddy.toml`; holds `TEMPLATE`. `find_legacy` locates a pre-rename `vibe.toml` so the error can name it. |
| `frameworks.py` | Preset registry (`REGISTRY`, 20 `Framework` entries), `detect()` and `render_cmd()`. The authority for every preset command and note. |
| `caddy.py` | Render the Caddyfile; Caddy admin-API control (`is_running`, `is_ours`, `foreign_instance`, `validate`, `reload`). |
| `launchd.py` | Render app plists; `launchctl` bootstrap/bootout/kickstart/print wrappers; log tailing. |
| `install.py` | System footprint: root LaunchDaemon, port preflight, Docker hint, CA trust/untrust, root handling. |
| `doctor.py` | Diagnostic checks, each returning a `Check` with a fix command. |
| `paths.py` | Every filesystem location and constant (ports, labels, TLD). `paths.home()` is the test seam; `data_dir()` and `state_dir()` resolve the XDG directories. |
| `ports.py` | Free-port probing on both loopback families, `lsof` holder lookup, auto-assignment in 3000-3999. |
| `names.py` | Route-name validation and slugification. |
| `gitwt.py` | Git worktree detection, branch and slug derivation. |
| `errors.py` | `VibeError` (with `hint`), `NotFound`, `Conflict`, `SetupRequired`. |
| `dashboard/` | FastAPI app (`app.py`), Jinja templates, static assets. JSON API under `/_api`, CSRF guard in middleware. |

## Dev loop

All tooling goes through uv. `uv format` (Ruff formatter) and `uv check` (ty) are uv-native preview commands, enabled by `[tool.uv] preview-features` in `pyproject.toml`. Ruff and ty are not dev dependencies; do not add them. The only dev dependencies are `pytest` and `pytest-cov`.

| Command | What it does |
| --- | --- |
| `just all` | `uv format`, `uv check`, tests. Run before every commit. |
| `just ci` | `uv format --check`, `uv check`, tests, `uv audit`. What CI runs. |
| `just test [ARGS]` | `uv run pytest [ARGS]` |
| `just cov` | Tests with coverage. |
| `just caddyfile` | Print the Caddyfile for the current registry. |
| `just validate` | Print it, then validate it with the real `caddy`. |
| `just dashboard [PORT]` | Run the dashboard in the foreground (default 7999). |
| `just install-cli` | `uv tool install --force --reinstall .` |

Python is `>=3.14`; the code uses `except A, B:` without parentheses (PEP 758) and `from __future__ import annotations`.

## Testing rules

- Every test must isolate `paths.home` to a tmp dir. `tests/conftest.py` does this with an autouse fixture (`isolated_home`) that monkeypatches `paths.home` to `tmp_path`, removes `SUDO_USER`, and replaces `launchd._run` with a stub that fails. Do not bypass it. Do not construct paths from `Path.home()` in tests.
- Never call the real `launchctl`, `security`, or `caddy reload`/`caddy trust`/`caddy run`. Use the `reloads` fixture to replace `caddy.reload` when code under test would call it. Anything new that shells out to a privileged or stateful tool needs a stub.
- The Caddy tests (`tests/test_caddy.py`) shell out to the real `caddy` binary for `caddy validate` and `caddy fmt --diff`. They skip when `caddy` is not installed. The generator must stay `caddy fmt` clean: tabs for indentation, exactly the spacing Caddy would produce. If you change `caddy.render`, run `just test tests/test_caddy.py` with Caddy installed.
- Do not touch the real `~/.local/share/vibe-caddy` or `~/.local/state/vibe-caddy`. For manual probing, use `HOME=/tmp/some-dir uv run vibe-caddy ...` (unset `XDG_DATA_HOME` and `XDG_STATE_HOME`, or point them into the tmp dir, since they take precedence over `HOME`), and remember that `service.register` calls `caddy.reload`, which will push to a live vibe-caddy Caddy on port 2019 if one is running.

## Conventions

- Line length 100 (`[tool.ruff] line-length`), target `py314`.
- Google-style docstrings (`Args:`, `Returns:`, `Raises:`) on public functions that need them.
- Comments explain why, not what.
- User-facing failures raise `VibeError` (or a subclass) with a `hint`; `cli.fail` prints them. Do not let tracebacks reach the user for expected conditions.
- Subprocess calls pass `timeout=` and `check=False` and handle the result explicitly.
- Dependencies: `fastapi`, `httpx2`, `jinja2`, `pydantic`, `rich`, `typer`, `uvicorn`. Keep the list short.

## Non-obvious gotchas

- **`*.localhost` resolves to `::1` first.** Caddy therefore binds both `127.0.0.1` and `::1` (`bind 127.0.0.1 ::1` in every block, `paths.BIND_HOSTS`). `ports.is_free` and `ports.is_listening` check both families for the same reason. A server bound to only one family can still collide with, or be unreachable from, the other.
- **Filesystem layout is XDG, split by durability.** `paths.data_dir()` (`$XDG_DATA_HOME/vibe-caddy`, default `~/.local/share/vibe-caddy`) holds what cannot be regenerated: `registry.json` and `caddy/` (Caddy's local CA and keys). `paths.state_dir()` (`$XDG_STATE_HOME/vibe-caddy`, default `~/.local/state/vibe-caddy`) holds everything derived: `Caddyfile`, `registry.lock`, `launchd/*.plist`, `log/*.log`. State is safe to delete; the next command rebuilds it. A relative `XDG_*` value is ignored, per the spec, and the fallback uses `paths.home()` so it resolves to the invoking user's home under `sudo`. XDG rather than `~/Library/Application Support` because XDG is the convention for a standalone CLI on macOS: Apple's location is for GUI apps and their companion CLIs, and unlike the XDG variables it cannot be redirected by the user. `gh`, `uv`, `gcloud` and `helm` do the same. Two paths stay outside XDG because launchd dictates them: `/Library/LaunchDaemons/dev.vibe-caddy.caddy.plist` (root daemon) and `~/Library/LaunchAgents/dev.vibe-caddy.<name>.plist` (the opt-in `autostart` symlink).
- **Legacy migration.** `install.migrate_legacy()` runs from `setup`: it moves `registry.json` and `caddy/` from the pre-XDG `~/.vibe-caddy` (`paths.legacy_dir()`) into the data dir, skips any target that already holds data, writes `MOVED.txt` in the old directory and does not delete it. Derived files are not migrated, since they embed stale absolute paths and are regenerated. `doctor` warns (`legacy layout`) while the old directory still holds a registry.
- **`doctor` check `daemon config path`** compares the `--config` path baked into the installed LaunchDaemon plist with `paths.caddyfile()`. They diverge when a custom `XDG_STATE_HOME` is set and `setup` runs under plain `sudo`, which resets the environment. The suggested fix is `sudo --preserve-env=XDG_STATE_HOME,XDG_DATA_HOME vibe-caddy setup`.
- **Caddy's data dir is pinned via `XDG_DATA_HOME`** to `<data dir>/caddy` (`paths.caddy_data_dir()`), in the LaunchDaemon plist, in `caddy.caddy_env()` and in `install.trust_ca`. Root's daemon and the user's `caddy trust` must share one CA, or the keychain trusts a root that signs nothing. `HOME` is pinned alongside it.
- **`paths.home()` follows `SUDO_USER`** when root, because `setup` runs under sudo but must write into the invoking user's data and state directories. `install.chown_to_user` hands both back afterwards.
- **App plists live in `<state dir>/launchd`, not `~/Library/LaunchAgents`**, so registering an app does not imply start-at-login. Only routes with `autostart = true` get a symlink into LaunchAgents (`launchd.write_plist` creates and removes it).
- **`caddy.is_ours()` exists so we never reconfigure someone else's Caddy.** It looks for the loopback `ask` listener (`paths.CADDY_ASK_PORT`, 2021) in the running config; every config we generate has it. `reload` refuses (or, with `require_running=False`, silently skips) when the admin port is held by something else. Do not remove the `ask` listener from the generated config without replacing this signature.
- **Each route lists both `https://host` and `http://host`**, so plain HTTP is proxied, not redirected.
- **Catch-all plus on-demand TLS.** Unregistered names fall through to a hostname-less block that proxies to the dashboard (route name `vibe`, `paths.DASHBOARD_ROUTE`) or returns a 404 string when the dashboard is not registered. Issuing certs for arbitrary names is gated by the `ask` endpoint, which admits only `*.localhost`.
- **`ws_origin_rewrite`** rewrites `Origin` and `Host` only on WebSocket upgrade requests (`@upgrade` matcher). Next.js HMR needs it.
- **Launchd job shape:** `<login shell> -lc <cmd>` so version-manager shims resolve. The shell is `paths.user_shell()`, the invoking user's login shell from the password database, not `$SHELL`: under `sudo` `$SHELL` is root's `/bin/sh`, which would not load the user's profile (nvm, pyenv, rbenv).  `KeepAlive: {SuccessfulExit: false}` with a 10 s throttle; env `PORT`, `PORT_<KEY>`, `VIBE_ROUTE`, `VIBE_URL`, `VIBE_HOSTNAME`.
- **Worktree routes** take the app name from the main checkout's `vibe-caddy.toml`, never inherit a pinned `port`, and are pruned only when the checkout's parent directory still exists (an unmounted volume must not wipe the registry).
- **On re-`start`, only `cmd`, `dir`, `icon`, `autostart`, `ws_origin_rewrite` are refreshed** from `vibe-caddy.toml`. `port` and `reserve_ports` are kept from the first registration.
- **`restart` also re-reads `vibe-caddy.toml`** (`service._refresh_from_project`): `cmd`, `icon`, `autostart` and `ws_origin_rewrite`, never `port`. It looks in the route's `dir`. A route with no `dir`, no file, or a file that fails `project.load` is restarted with its stored values, so a half-edited file cannot block a restart. Earlier docs claimed `restart` picked up edits; it did not.
- **No `vibe.toml` fallback.** The file was renamed to `vibe-caddy.toml` (`project.CONFIG_NAME`). `start_project` raises `NotFound` naming a leftover `vibe.toml` and the exact `mv` command, only when no `vibe-caddy.toml` is found. Honouring both names would leave two files that can disagree.
- **Framework presets exist because a command that ignores `$PORT` looks like a working app.** The app binds the framework default, Caddy proxies to the assigned port, and the route answers nothing. In `frameworks.py`, commands start with `exec` and call project-local binaries (`./node_modules/.bin/vite`, `bin/rails`) instead of `npm run`/`bundle exec`, because a wrapper stays the PID launchd tracks while the real server is an untracked child. Re-exec auto-reloaders (Django, Flask, FastAPI `dev`, Nuxt's fork) are disabled for the same reason. Servers bind `127.0.0.1` explicitly, since `localhost` can resolve to `::1` while Caddy dials `127.0.0.1`. `express` and `go` cannot pass the port; they set `reads_port_env` and carry a note. `{host}` and `{url}` in a template are filled by `render_cmd`; `{{ }}` in notes are literal braces.
- **`init` and presets:** plain `init` runs `frameworks.detect` and writes the file without starting. `-w NAME|auto` writes and then calls `service.start_project`. `-w list` exits before touching the directory. An explicit `--cmd` beats the preset's command. The port is never written to the file. Detection ties break on `Framework.priority` (Next and SvelteKit over Vite, FastAPI over uvicorn); content checks (`detect_contains`) confirm ambiguous marker files such as `manage.py`.
- **Dashboard CSRF guard** (`dashboard/app.py`): the API is unauthenticated and route creation runs shell commands. Non-safe methods are refused on `Sec-Fetch-Site: cross-site` or an untrusted `Origin`. Bookmark hostnames are intentionally excluded from trusted origins. A missing `Origin` is allowed (CLI, curl). Keep it that way when adding endpoints.
- **`deregister --all` spares the dashboard.** `service.removable()` filters out `paths.DASHBOARD_ROUTE` unless asked, and orders worktree routes before their parent so a parent is never dropped while a child still points at it. Bulk removal goes through `service.deregister_many()`, which edits the registry once and reloads Caddy once; looping `deregister()` would publish every intermediate state and is N times the work.
- **Names:** lowercase DNS labels, at most one dot (`<worktree>.<app>`), `local` and `localhost` reserved.

## Known inconsistencies in the code

- `README.md` in `pyproject.toml` (`readme = "README.md"`) is the file you are reading the sibling of; keep it accurate, since it is the package description.
