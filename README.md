# vibe-caddy

Give each local dev server a stable `https://<name>.localhost` address, with a trusted certificate, WebSocket support, and automatic restart on crash.

vibe-caddy is a stateless command-line tool. It does not run a daemon of its own. It writes configuration for three things macOS and Homebrew already provide:

| Concern | Handled by | How |
| --- | --- | --- |
| Name resolution | macOS resolver | `*.localhost` resolves to loopback at any depth, so `feature-auth.myapp.localhost` works with no setup. |
| Ports 80 and 443, TLS, proxying | [Caddy](https://caddyserver.com) | Runs as a root LaunchDaemon bound to loopback only. Certificates come from Caddy's local CA, trusted in the System keychain. Handles reverse proxying and WebSocket upgrades. |
| Running your apps | launchd | One job per app: restart on crash, log redirection, PID tracking. vibe-caddy writes a plist and calls `launchctl bootstrap`. |

What vibe-caddy does **not** install: no dnsmasq, no `/etc/resolver` file, no `/etc/hosts` entries, no pf (packet filter) rules, no helper binary, and no resident process of its own.

`registry.json` in the data directory (`~/.local/share/vibe-caddy`) is the source of truth. The Caddyfile and the launchd plists are derived from it, live in the state directory (`~/.local/state/vibe-caddy`), and are regenerated on every change. Both directories follow the XDG Base Directory spec, as `gh`, `uv`, `gcloud` and `helm` do; see [Runtime files](#runtime-files).

This is a rewrite of the Go tool `local.vibe` (`https://<name>.vibe`). That tool needed dnsmasq, a resolver file, pf `rdr` rules patched into `/etc/pf.conf`, and a root helper. The pf layer was the fragile part: rules silently disappeared whenever anything else reloaded pf, and macOS 26 broke `rdr` inside an anchor on lo0. Nothing here touches pf.

## Requirements

- macOS.
- Caddy: `brew install caddy`.
- [uv](https://docs.astral.sh/uv/) 0.12.23 or newer. uv is required; there is no non-uv install path. The project needs Python 3.14, which uv fetches on its own.

## Install

```bash
git clone https://github.com/yolabingo/vibe-caddy
cd vibe-caddy
uv tool install .
sudo vibe-caddy setup
```

If `sudo` cannot find the command, use `sudo "$(which vibe-caddy)" setup`.

`setup` does the following, in order:

1. Checks that `caddy` is on `PATH`.
2. Checks that ports 80 and 443 are free. If either is held, it prints the holder and exits having changed nothing. A Docker container publishing 80 or 443 is the usual cause; setup names the container and suggests `docker stop <name>`.
3. Checks that nothing foreign holds Caddy's admin port `127.0.0.1:2019`. If something does, it exits having changed nothing.
4. Migrates a pre-XDG `~/.vibe-caddy` if one exists (see [Upgrading from `~/.vibe-caddy`](#upgrading-from-vibe-caddy)).
5. Writes `~/.local/state/vibe-caddy/Caddyfile` and validates it with `caddy validate`.
6. Writes `/Library/LaunchDaemons/dev.vibe-caddy.caddy.plist` and loads it with `launchctl bootstrap system`.
7. Waits up to 15 seconds for Caddy's admin API.
8. Makes one request so Caddy generates its local CA, then trusts that CA in the System keychain (`caddy trust`, with a direct `security add-trusted-cert` fallback). Skip this step with `--no-trust`.
9. Registers and starts the web dashboard at `https://vibe.localhost`.
10. Gives the data and state directories back to your user, and reclaims the root-owned
   `__pycache__` directories that running under `sudo` leaves in the uv tool
   directory (they would otherwise break the next `uv tool install --force`).

If the dashboard fails to start, setup says so and still succeeds: routing does
not depend on it. Re-run that step on its own with:

```bash
vibe-caddy dashboard install
```

Without a running dashboard an unregistered `*.localhost` name returns a plain
`No vibe-caddy route for <host>.` 404. With it, you get a page listing your routes.

## Quickstart

```bash
cd ~/code/myapp
vibe-caddy init -w auto      # detects the framework, writes ./vibe-caddy.toml, starts the app
vibe-caddy open myapp        # or visit https://myapp.localhost
```

To review the command before anything runs, use plain `init`, which writes the file without starting the app:

```bash
vibe-caddy init              # writes ./vibe-caddy.toml with a detected cmd
$EDITOR vibe-caddy.toml
vibe-caddy start             # registers the app and starts it
```

`init` takes the route name from the directory name (`--name` overrides it) and detects the framework (see [Frameworks](#frameworks)); with no match it defaults `cmd` to `npm run dev`. It refuses to overwrite an existing `vibe-caddy.toml` unless you pass `--overwrite`.

`start` with no argument finds `vibe-caddy.toml` in the current directory or any parent, registers the app on first run, assigns a free port from 3000-3999, and bootstraps the launchd job. Run it again after editing `vibe-caddy.toml` and the `cmd`, `icon`, `autostart` and `ws_origin_rewrite` changes are applied. The assigned port is kept; change it with `vibe-caddy update <name> --port <n>` (or `--port 0` to have a free one assigned).

`vibe-caddy restart <name>` also re-reads `vibe-caddy.toml` before restarting, so editing `cmd` and restarting is enough. It keeps the assigned port. A missing or syntactically broken file is not an error: the app restarts with its last known-good command.

The project file was previously named `vibe.toml`. There is no backward compatibility: if `start` finds a `vibe.toml` and no `vibe-caddy.toml`, it stops with an error that names the file and the fix, which is `mv vibe.toml vibe-caddy.toml`.

## The one rule: bind `$PORT`

**Your `cmd` must listen on the port vibe-caddy assigns, which arrives in the environment as `$PORT`.**

vibe-caddy picks a port, writes it into the Caddyfile as the upstream, and exports it as `$PORT` to your command. A framework that ignores `$PORT` and binds its own default (3000, 5173, 5000, 8000) listens on a different port than the one Caddy proxies to. The app starts, launchd reports a running process, and every request fails. `vibe-caddy list` shows the state `starting` in that case.

Per-framework commands and the configuration each framework still needs (allowed hosts, trusted origins) are in the [Frameworks](#frameworks) table; `vibe-caddy init -w NAME` writes the right command for you. If you write `cmd` by hand, pass the port explicitly, use `exec`, call the project-local binary, and bind `127.0.0.1`, for the reasons given there.

HMR for Next.js works because vibe-caddy rewrites `Origin` to the upstream's own origin on WebSocket upgrades only. Next.js refuses upgrades from an unknown origin, and its HMR client reloads the page after repeated failures.

The `Origin` rewrite is on by default for every proxied route and can be turned off per app with `ws_origin_rewrite = false`.

Your `cmd` runs as `<login shell> -lc "<cmd>"`, in the directory that holds `vibe-caddy.toml`, so nvm, pyenv and rbenv shims are on `PATH`. The login shell is your own, read from the password database rather than `$SHELL` (under `sudo`, `$SHELL` is root's `/bin/sh`, which would not load your profile); it falls back to `$SHELL`, then `/bin/zsh`. The environment contains:

| Variable | Value |
| --- | --- |
| `PORT` | The assigned port. |
| `PORT_<KEY>` | One per `reserve_ports` entry, key uppercased. |
| `VIBE_ROUTE` | Route name, e.g. `myapp`. |
| `VIBE_URL` | `https://myapp.localhost` |
| `VIBE_HOSTNAME` | `myapp.localhost` |

stdout and stderr go to `~/.local/state/vibe-caddy/log/<name>.log`; read them with `vibe-caddy logs <name>`.

launchd restarts the command if it exits non-zero, at most once every 10 seconds. A clean exit (status 0) is not restarted.

## Frameworks

`vibe-caddy init` ships 20 presets, one per common dev server. A preset is a start command that is known to bind `$PORT`, so you do not have to work out which flag your framework needs.

```bash
vibe-caddy init -w auto       # detect the framework, write vibe-caddy.toml, register and start
vibe-caddy init -w vite       # use a named preset, then register and start
vibe-caddy init               # detect and write the file only; edit it, then run `vibe-caddy start`
vibe-caddy init -w list       # print every preset and its command, then exit
```

| Invocation | Effect |
| --- | --- |
| `init -w NAME` | Use that preset, write `vibe-caddy.toml`, then register and start the app. |
| `init -w auto` | Detect the framework first, then the same. Fails if nothing is detected. |
| `init -w list` | Print every preset and its command, then exit. |
| `init` | Detect the framework so the generated `cmd` is a working one, write the file, and stop. It does not start the app. With no match, `cmd` is `npm run dev`. |
| `--cmd CMD` | Overrides the preset's command. |
| `--overwrite` | Replace an existing `vibe-caddy.toml`. `init` refuses otherwise. |

The port is never written into the file. It is assigned on every start, so worktrees of one repository do not collide.

### Why presets exist

A start command that ignores the assigned port is the most common way this tool appears broken: the app binds the framework's default, Caddy proxies to the assigned port, and the route answers nothing. Every preset passes the port explicitly where the framework allows it. The commands follow three rules:

- **They use `exec` and call project-local binaries directly** (`./node_modules/.bin/vite`, `bin/rails`) rather than `npm run dev` or `bundle exec`. A package-manager wrapper stays alive as the process launchd tracks, while the real server runs as a child launchd cannot see.
- **Auto-reloaders that re-exec are disabled** (Django `--noreload`, Flask `--no-reload`, FastAPI `dev --no-reload`, Nuxt `--no-fork`), for the same reason: the supervised PID must be the server.
- **Everything that can be told binds `127.0.0.1` explicitly.** A default of `localhost` can resolve to `::1` only, while Caddy dials `127.0.0.1`.

`express` and `go` are the two presets whose command cannot pass the port. Your application must read `PORT` from the environment itself. Both are marked `reads_port_env` in the registry and print a note saying so.

### Detection

Detection reads `package.json` dependencies (all sections), `pyproject.toml` dependencies (including optional ones), and marker files such as `angular.json`, `go.mod` and `config/application.rb`. A marker file can require a content check: `manage.py` must mention Django, and the Rails files must mention `rails`. When several presets match, the one with the highest priority wins, so Next beats Vite, SvelteKit beats Vite, and FastAPI beats bare uvicorn.

### Presets

Commands are printed by `vibe-caddy init --framework list`. `<name>` is the route name; Angular and Hugo are given the route's hostname.

```
NAME          FRAMEWORK                COMMAND                                  
angular       Angular                  exec ./node_modules/.bin/ng serve --host 
                                       127.0.0.1 --port $PORT --allowed-hosts   
                                       <name>.localhost                         
astro         Astro                    exec ./node_modules/.bin/astro dev       
                                       --ignore-lock --host 127.0.0.1 --port    
                                       $PORT                                    
django        Django                   exec python3 -u manage.py runserver      
                                       --noreload 127.0.0.1:$PORT               
express       Express / plain Node     exec node server.js                      
fastapi       FastAPI                  exec fastapi dev --no-reload --host      
                                       127.0.0.1 --port $PORT                   
flask         Flask                    exec flask run --host 127.0.0.1 --port   
                                       $PORT --no-reload --no-debugger          
go            Go                       go build -o .vibe-caddy-bin . && exec    
                                       ./.vibe-caddy-bin                        
gradio        Gradio                   exec env GRADIO_SERVER_NAME=127.0.0.1    
                                       GRADIO_SERVER_PORT=$PORT                 
                                       GRADIO_NUM_PORTS=1 python3 -u app.py     
hugo          Hugo                     exec hugo server --bind 127.0.0.1 --port 
                                       $PORT --baseURL https://<name>.localhost 
                                       --appendPort=false --liveReloadPort 443  
jekyll        Jekyll                   exec bundle exec jekyll serve --host     
                                       127.0.0.1 --port $PORT                   
next          Next.js                  exec ./node_modules/.bin/next dev        
                                       --hostname 127.0.0.1 --port $PORT        
nuxt          Nuxt                     exec ./node_modules/.bin/nuxt dev --host 
                                       127.0.0.1 --port $PORT --strictPort      
                                       --no-fork --no-tui                       
php           PHP built-in server      exec php -S 127.0.0.1:$PORT -t public    
react-router  React Router (ex-Remix)  exec ./node_modules/.bin/react-router dev
                                       --host 127.0.0.1 --port $PORT            
                                       --strictPort                             
rails         Ruby on Rails            exec bin/rails server -b 127.0.0.1 -p    
                                       $PORT -P tmp/pids/vibe-$PORT.pid         
static        Static files             exec python3 -u -m http.server $PORT     
                                       --bind 127.0.0.1 --directory .           
streamlit     Streamlit                exec streamlit run app.py --server.port  
                                       $PORT --server.address 127.0.0.1         
                                       --server.headless true                   
                                       --browser.gatherUsageStats false         
sveltekit     SvelteKit                exec ./node_modules/.bin/vite dev --host 
                                       127.0.0.1 --port $PORT --strictPort      
uvicorn       Uvicorn (bare ASGI)      exec uvicorn main:app --host 127.0.0.1   
                                       --port $PORT                             
vite          Vite                     exec ./node_modules/.bin/vite --host     
                                       127.0.0.1 --port $PORT --strictPort
```

The table below lists what each preset leaves for you to configure. The same notes are printed after `init` writes the file.

| Preset | Framework | Configuration you still apply |
| --- | --- | --- |
| `angular` | Angular | None. |
| `astro` | Astro | Add `server: { allowedHosts: ['.localhost'] }` to astro.config. Astro has no strict-port flag: if the port is taken it moves to another one. |
| `django` | Django | Add `ALLOWED_HOSTS = [".localhost"]` to settings (the leading dot covers subdomains). Add `CSRF_TRUSTED_ORIGINS = ["https://*.localhost"]`; the scheme is required. |
| `express` | Express / plain Node | Your code must read process.env.PORT; nothing passes it for you. Do not use nodemon or node --watch: both supervise a child process. |
| `fastapi` | FastAPI | Needs the fastapi[standard] extra for the `fastapi` command. |
| `flask` | Flask | Set --app if your application is not in app.py or wsgi.py. |
| `go` | Go | Your code must read os.Getenv("PORT"); nothing passes it for you. Add .vibe-caddy-bin to .gitignore. |
| `gradio` | Gradio | Remove any server_port= argument in launch(); it overrides the environment. |
| `hugo` | Hugo | None. |
| `jekyll` | Jekyll | Do not add --livereload: its fixed port 35729 collides between sites. |
| `next` | Next.js | Add `allowedDevOrigins: ['*.localhost']` to next.config for HMR from this host. |
| `nuxt` | Nuxt | Add `vite: { server: { allowedHosts: ['.localhost'] } }` to nuxt.config. |
| `php` | PHP built-in server | Change -t if your document root is not public/. |
| `react-router` | React Router (ex-Remix) | Vite blocks unknown Host headers. Add `server: { allowedHosts: ['.localhost'] }` to your Vite config. |
| `rails` | Ruby on Rails | Add `config.hosts << ".localhost"` to config/environments/development.rb. |
| `static` | Static files | None. |
| `streamlit` | Streamlit | None. |
| `sveltekit` | SvelteKit | Vite blocks unknown Host headers. Add `server: { allowedHosts: ['.localhost'] }` to your Vite config. |
| `uvicorn` | Uvicorn (bare ASGI) | Change main:app if your application object lives elsewhere. |
| `vite` | Vite | Vite blocks unknown Host headers. Add `server: { allowedHosts: ['.localhost'] }` to your Vite config. |

## `vibe-caddy.toml` reference

Unknown keys are an error.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | required | Route name; the app is served at `https://<name>.localhost`. A lowercase DNS label: letters, digits and inner hyphens, at most 63 characters. `local` and `localhost` are reserved. |
| `cmd` | string | required | Shell command that starts the dev server. Must bind `$PORT`. |
| `port` | integer, 1-65535 | auto | Pin a port instead of taking one from 3000-3999. Ignored for git worktrees, whose ports are always auto-assigned. |
| `icon` | string | none | Emoji or image URL shown in the dashboard. |
| `autostart` | boolean | `false` | Also symlink the plist into `~/Library/LaunchAgents` so the app starts at login. |
| `ws_origin_rewrite` | boolean | `true` | Rewrite `Origin` and `Host` to the upstream's on WebSocket upgrade requests. |
| `reserve_ports` | table of string to integer | empty | Extra ports your command binds. Each key `foo` is exported as `$PORT_FOO`. A value of `0`, or a key left out of the value, means auto-assign; a number pins that port. Keys must be valid identifiers. |

```toml
name = "myapp"
cmd = "npm run dev"
# port = 3000
# icon = "zap"
# autostart = false

[reserve_ports]
websocket = 0   # exported as $PORT_WEBSOCKET
```

## Route types

Every route is one entry in the registry and one site block in the Caddyfile. A route name has at most one dot (`<worktree>.<app>`).

| Type | Created by | Lifecycle |
| --- | --- | --- |
| `managed` | `vibe-caddy start` from a `vibe-caddy.toml` (or the dashboard API with a `cmd`) | launchd runs the `cmd`. `start`, `stop`, `restart` and `logs` apply. Survives `stop`; removed by `deregister`. |
| `worktree` | `vibe-caddy start` inside a linked git worktree | Same as managed, with its own name and port. Removed by `deregister` or `prune`. |
| `static` | `vibe-caddy register <name> <port>` | Maps a name to a port whose process you run yourself. vibe-caddy never starts or stops it. State is `up` or `down` depending on whether the port answers. |
| `bookmark` | `vibe-caddy register <name> --url <url>` | Maps a name to an external URL. By default it is a 307 redirect that keeps the path and query. With `--proxy` it is a reverse proxy, so `<name>.localhost` stays in the address bar. `--insecure` accepts a self-signed upstream certificate. |

Proxied bookmarks strip `Origin`, `Referer` and `X-Forwarded-For` from the request and the `Domain=` attribute from `Set-Cookie`, because many appliances reject or mis-scope those.

## Git worktrees

Running `vibe-caddy start` in a linked worktree registers a separate route on its own auto-assigned port:

```
https://<branch-slug>.<app>.localhost
```

- `<app>` comes from the `name` in the **main** checkout's `vibe-caddy.toml`, not the worktree's copy, so a copied file cannot detach the worktree from its app.
- `<branch-slug>` is the current branch, lowercased with non-alphanumerics turned into hyphens and a leading `worktree-` removed. A detached HEAD falls back to the directory name.
- `vibe-caddy start --as <slug>` overrides the slug.
- `vibe-caddy prune` deregisters worktree routes whose checkout directory is gone. It skips a route when the checkout's parent directory is also missing, so an unmounted volume does not wipe your routes.
- `vibe-caddy doctor` warns about worktree routes that `prune` would remove.

## CLI reference

Run `vibe-caddy <command> --help` for any command. `-V` / `--version` prints the version. Commands that need root say so. `vibe-caddy --help` groups the commands into the same five panels used below.

### Inspecting

| Command | Description |
| --- | --- |
| `vibe-caddy list` | List every registered route and its live state (`ready`, `starting`, `crashed`, `stopped` for managed; `up`, `down` for static and bookmark). |
| `vibe-caddy status` | Show whether Caddy is serving, paths to the Caddyfile, registry and state directory, and a route count. Exits 1 if Caddy is not running or is not ours. |
| `vibe-caddy open [name]` | Open a route in the browser. Without a name, opens the dashboard. |
| `vibe-caddy logs <name> [-n LINES] [-f]` | Show a managed route's log. `-n` defaults to 50; `-f` follows. |

### Running apps

| Command | Description |
| --- | --- |
| `vibe-caddy init [--name NAME] [-w NAME] [--cmd CMD] [-d PATH] [--overwrite]` | Write a starter `vibe-caddy.toml` in the current directory, detecting the framework. With `-w NAME` or `-w auto`, also register and start the app; `-w list` prints the presets. `-d`/`--directory` sets up a project other than the current directory. |
| `vibe-caddy start [name] [--as SLUG]` | With a name, start a registered managed route. Without one, read `./vibe-caddy.toml` (searching upward), register the app if needed, and start it. |
| `vibe-caddy stop <name>` | Stop a managed app. The route stays registered. |
| `vibe-caddy restart <name>` | Re-read `vibe-caddy.toml` (`cmd`, `icon`, `autostart`, `ws_origin_rewrite`), rewrite the plist and restart the job. Keeps the port; falls back to the last known-good command if the file is missing or invalid. |

### Managing routes

| Command | Description |
| --- | --- |
| `vibe-caddy register <name> [port] [--url URL] [--proxy] [--insecure] [--icon ICON]` | Point a name at a port you start yourself, or at an external URL. Needs a port or `--url`. |
| `vibe-caddy update <name>` | Change a route in place: `--port`, `--cmd`, `--icon`, `--autostart/--no-autostart`. |
| `vibe-caddy deregister <name>` | Remove a route, stopping it first if managed. |
| `vibe-caddy deregister --all [--yes] [--include-dashboard]` | Remove every registered app. Lists what it will remove and asks first; `--yes` skips the prompt, `--include-dashboard` also removes vibe-caddy's own dashboard route. |
| `vibe-caddy prune` | Remove worktree routes whose checkout is gone. |

### Proxy and dashboard

| Command | Description |
| --- | --- |
| `vibe-caddy reload` | Regenerate the Caddyfile from the registry and reload Caddy. |
| `vibe-caddy caddyfile [--validate]` | Print the generated Caddyfile, or with `--validate` write it and run `caddy validate`. |
| `sudo vibe-caddy caddy start` | Write the plist and load the Caddy LaunchDaemon. |
| `sudo vibe-caddy caddy stop` | Unload the Caddy LaunchDaemon and delete its plist. |
| `sudo vibe-caddy caddy restart` | Restart the Caddy LaunchDaemon in place. |
| `vibe-caddy dashboard install` | Register the dashboard as a managed route at `https://vibe.localhost`. |
| `vibe-caddy dashboard serve [--port 7999] [--host 127.0.0.1]` | Run the dashboard in the foreground. Used by its launchd job. |

### Installation and diagnostics

| Command | Description |
| --- | --- |
| `sudo vibe-caddy setup [--no-trust]` | Install the LaunchDaemon, trust the CA, and start the dashboard. |
| `sudo vibe-caddy uninstall` | Remove the LaunchDaemon and untrust the CA. |
| `vibe-caddy doctor` | Run every check and print a fix for each failure. Exits 1 on any failure. |

`doctor` checks: the `caddy` binary, that `*.localhost` resolves to loopback, the LaunchDaemon plist and job, that the `--config` path in the installed plist matches the path the CLI computes (`daemon config path`), Caddy's admin API (and that it is ours), listeners on 80 and 443, the generated Caddyfile, the CA and its keychain trust, managed routes that crashed or never bound `$PORT`, worktree routes whose checkout is gone, and a leftover pre-XDG `~/.vibe-caddy` that still holds a registry (`legacy layout`).

## Generated Caddyfile

Each route becomes one site block bound to loopback on both address families (`*.localhost` resolves to `::1` first). This is real output for a managed app, a proxied bookmark and the dashboard (global options and the on-demand-TLS gate trimmed to the essentials):

```caddyfile
{
	admin 127.0.0.1:2019
	local_certs
	on_demand_tls {
		ask http://127.0.0.1:2021/check
	}
}

https://myapp.localhost, http://myapp.localhost {
	bind 127.0.0.1 ::1
	@upgrade {
		header Connection *Upgrade*
		header Upgrade websocket
	}
	reverse_proxy @upgrade 127.0.0.1:3000 {
		header_up Origin http://127.0.0.1:3000
		header_up Host 127.0.0.1:3000
	}
	reverse_proxy 127.0.0.1:3000
}

https://nas.localhost, http://nas.localhost {
	bind 127.0.0.1 ::1
	reverse_proxy https://nas.example.com {
		header_up -Origin
		header_up -Referer
		header_up -X-Forwarded-For
		header_down Set-Cookie (?i);\s*domain=[^;]* ''
	}
}

# Anything not registered above: the dashboard's unknown-name page.
https://, http:// {
	bind 127.0.0.1 ::1
	tls {
		on_demand
	}
	reverse_proxy 127.0.0.1:3001
}
```

Both the `https://` and `http://` addresses are listed per route, so plain HTTP is served by the same proxy rather than redirected to HTTPS. The catch-all block mints certificates on demand for unregistered names; Caddy asks a loopback-only endpoint on port 2021 first, which approves only names under `.localhost`.

Do not edit the Caddyfile by hand. It is overwritten on every change. Run `vibe-caddy caddyfile` to see what would be generated.

## Runtime files

vibe-caddy follows the XDG Base Directory spec. `$XDG_DATA_HOME` defaults to `~/.local/share` and `$XDG_STATE_HOME` to `~/.local/state`; a relative value is ignored, as the spec requires. The split is by durability:

- The data directory, `$XDG_DATA_HOME/vibe-caddy`, holds what cannot be regenerated: the registry and Caddy's local CA.
- The state directory, `$XDG_STATE_HOME/vibe-caddy`, holds everything derived from those. It is safe to delete; the next command rebuilds it.

| Path | Purpose |
| --- | --- |
| `~/.local/share/vibe-caddy/registry.json` | Source of truth: every route, plus dashboard preferences. |
| `~/.local/share/vibe-caddy/caddy/` | Caddy's data directory. The CA root is `caddy/caddy/pki/authorities/local/root.crt` beneath it. |
| `~/.local/state/vibe-caddy/registry.lock` | Lock held during registry changes. |
| `~/.local/state/vibe-caddy/Caddyfile` | Generated Caddy configuration. |
| `~/.local/state/vibe-caddy/launchd/dev.vibe-caddy.<name>.plist` | Generated launchd job for each managed app. |
| `~/.local/state/vibe-caddy/log/<name>.log` | stdout and stderr of a managed app. |
| `~/.local/state/vibe-caddy/log/caddy.log` | Caddy's own log (INFO). |
| `~/.local/state/vibe-caddy/log/caddy.out.log`, `caddy.err.log` | stdout and stderr of the Caddy daemon. Check `caddy.err.log` if Caddy will not start. |
| `/Library/LaunchDaemons/dev.vibe-caddy.caddy.plist` | The root LaunchDaemon that runs Caddy. |
| `~/Library/LaunchAgents/dev.vibe-caddy.<name>.plist` | Symlink to the app's plist; exists only for routes with `autostart = true`. |

The last two paths are outside XDG because launchd dictates them. XDG is used rather than `~/Library/Application Support` because it is the convention for a standalone command-line tool: Apple's location is meant for GUI apps and their companion CLIs, and unlike the XDG variables it cannot be redirected.

App plists live in `~/.local/state/vibe-caddy/launchd`, not `~/Library/LaunchAgents`, so that registering an app does not make it start at login.

### Upgrading from `~/.vibe-caddy`

Earlier releases kept everything in `~/.vibe-caddy`. `sudo vibe-caddy setup` migrates it: it moves `registry.json` and `caddy/` into the data directory, writes a `MOVED.txt` in the old directory, and does not delete it. Derived files (Caddyfile, plists, logs) are not migrated; they are regenerated. A target that already holds data is not overwritten. Until you migrate, `vibe-caddy doctor` warns about the old directory. Once it has moved you can delete `~/.vibe-caddy`.

## What setup changes on your Mac

| Change | Location |
| --- | --- |
| LaunchDaemon plist, loaded in the `system` domain | `/Library/LaunchDaemons/dev.vibe-caddy.caddy.plist` |
| Generated Caddyfile | `~/.local/state/vibe-caddy/Caddyfile` |
| Caddy local CA added as a trusted root | System keychain (certificate named `Caddy Local Authority`) |
| Directories for the registry and Caddy's data | `~/.local/share/vibe-caddy/` |
| Directories for generated config, plists and logs | `~/.local/state/vibe-caddy/` |

`sudo vibe-caddy uninstall` reverses the first and third: it unloads and deletes the LaunchDaemon plist and removes every `Caddy Local Authority` certificate from the System keychain. It names two paths it leaves in place: `~/.local/share/vibe-caddy` (registry, certificate authority) and `~/.local/state/vibe-caddy` (generated config, logs). Delete them by hand if you want them gone. Apps that are still running under launchd keep running until you `vibe-caddy stop` them. To remove the CLI: `uv tool uninstall vibe-caddy`.

`sudo vibe-caddy caddy stop` unloads the daemon and deletes its plist but leaves the CA trusted; `sudo vibe-caddy caddy start` writes the plist again and loads it.

## Troubleshooting

Start with `vibe-caddy doctor`. Each failing check prints the command that fixes it.

**Setup says port 443 (or 80) is held.** Setup refuses before changing anything and names the holder. A Docker container publishing 80/443 is the common case; setup finds the container and prints `docker stop <name>`. Stop it, or change its published ports, then re-run `sudo vibe-caddy setup`.

**A foreign Caddy is on port 2019.** `status` shows `not ours`, and `setup`, `reload` and `doctor` report that another Caddy holds `127.0.0.1:2019`. vibe-caddy refuses to reconfigure a Caddy it did not start. Pushing a config into someone else's instance would either fail (an unprivileged Caddy cannot bind 443) or silently replace the config they are running. Ownership is detected by looking for vibe-caddy's loopback `ask` listener (port 2021) in the running config. Stop the other Caddy (a stray `caddy run`, or a container publishing 2019), then run `sudo vibe-caddy setup`. Registry changes still succeed in the meantime; they are just not served yet.

**Doctor reports `daemon config path` as a mismatch.** The `--config` path in the installed LaunchDaemon plist differs from the path the CLI now computes. This happens when you set a custom `XDG_STATE_HOME` or `XDG_DATA_HOME` and run setup under plain `sudo`, which resets the environment, so root resolved different directories. If you redirect the XDG variables, preserve them through sudo: `sudo --preserve-env=XDG_STATE_HOME,XDG_DATA_HOME vibe-caddy setup`.

**The browser shows a certificate warning.** The CA is not trusted. Run `vibe-caddy doctor`; if `tls ca` fails, run `sudo vibe-caddy setup` again (do not pass `--no-trust`). Restart the browser afterwards. If doctor says Caddy has not generated its CA yet, visit any `https://<name>.localhost` once and re-run setup.

**The app starts but nothing answers.** Almost always the `$PORT` rule: the command bound a different port. `vibe-caddy list` shows `starting`. Read `vibe-caddy logs <name>`, see which port the framework announced, and pass `$PORT` explicitly (table above). Then `vibe-caddy restart <name>`. A state of `crashed` means the process exited; the log says why.

**Vite returns "Blocked request. This host is not allowed."** Set `server.allowedHosts: ['.localhost']` in `vite.config`.

**A name shows the unknown-name page or a 404.** The route is not registered. Check `vibe-caddy list`, and note that names are lowercase.

## Security

The dashboard's JSON API is unauthenticated, and creating a route runs a shell command as you. It therefore binds `127.0.0.1` only, and refuses state-changing requests (anything other than GET, HEAD, OPTIONS) from a browser context it does not trust:

- requests with `Sec-Fetch-Site: cross-site` are rejected;
- requests with an `Origin` header that is not loopback, `vibe.localhost` or a registered non-bookmark route are rejected.

Bookmark routes are deliberately not trusted origins: they front an upstream you do not control, and that upstream must not be able to drive the API from your browser. Requests with no `Origin` header (curl, scripts) are allowed, so any local process can reach the API. Responses carry a restrictive `Content-Security-Policy`.

Caddy listens on loopback only, so dev servers are not exposed to your LAN.

## Development

See `CLAUDE.md` for the module map, test rules and `just` tasks.
