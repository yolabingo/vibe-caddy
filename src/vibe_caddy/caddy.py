"""Caddyfile generation and Caddy admin-API control.

Caddy owns everything the previous implementation hand-rolled: the HTTP and HTTPS
listeners, certificate issuance and renewal from a local CA, reverse proxying,
WebSocket upgrades, and the plain-HTTP to HTTPS redirect. This module's whole job
is to turn the registry into a Caddyfile and ask Caddy to load it.

Three details are worth knowing:

* **Catch-all plus on-demand TLS.** Registered routes get an explicit site block.
  Everything else falls through to a hostname-less block that shows the dashboard's
  "unknown name" page. Serving an arbitrary unregistered hostname over HTTPS means
  minting a certificate for a name we have never seen, so on-demand issuance is
  enabled and gated by an ``ask`` endpoint -- which Caddy serves to itself on
  loopback, admitting only names under ``.vc.localhost``.

* **Origin rewriting on upgrades.** Dev servers increasingly reject WebSocket
  upgrades whose ``Origin`` is not one they recognise. Next.js's HMR client reloads
  the whole page after three such failures, which silently wipes component state
  every twenty seconds. Rewriting ``Origin`` to the upstream's own origin on
  upgrade requests only -- leaving ordinary requests untouched -- avoids that.

* **Only our own instance is reconfigured.** Another Caddy on the admin port is
  left strictly alone; see :func:`is_ours`.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx2

from . import paths, ports
from .errors import SetupRequired, VibeError
from .models import RegistryData, Route, RouteType

#: Upstream address for a locally-bound dev server.
_LOOPBACK = "127.0.0.1"


# ------------------------------------------------------------------ rendering


def _bind_line(indent: str = "\t") -> str:
    return f"{indent}bind {' '.join(paths.BIND_HOSTS)}\n"


def _reverse_proxy(route: Route, indent: str = "\t") -> str:
    """Render the proxy directives for a route that fronts a local port."""
    upstream = f"{_LOOPBACK}:{route.port}"
    if not route.ws_origin_rewrite:
        return f"{indent}reverse_proxy {upstream}\n"

    return (
        f"{indent}@upgrade {{\n"
        f"{indent}\theader Connection *Upgrade*\n"
        f"{indent}\theader Upgrade websocket\n"
        f"{indent}}}\n"
        f"{indent}reverse_proxy @upgrade {upstream} {{\n"
        f"{indent}\theader_up Origin http://{upstream}\n"
        f"{indent}\theader_up Host {upstream}\n"
        f"{indent}}}\n"
        f"{indent}reverse_proxy {upstream}\n"
    )


def _proxy_target(route: Route, indent: str) -> tuple[str, str]:
    """Split a proxied bookmark's URL into an upstream and the rewrite that reaches it.

    ``reverse_proxy`` takes an upstream of ``scheme://host:port`` and rejects
    anything with a path, so a bookmark like ``https://example.com/docs/`` cannot be
    handed over whole. The authority becomes the upstream and the path (and query)
    become an explicit ``rewrite`` that prefixes every request, which is what
    "proxy to that URL" means: ``/x`` on the ``.vc.localhost`` name reaches
    ``/docs/x`` upstream. A bare ``/`` or no path needs no rewrite.

    Returns:
        The upstream, and the rewrite directive line (empty when not needed).
    """
    parts = urlsplit(str(route.url))
    # Keep the authority exactly as entered (explicit port, IPv6 brackets) rather
    # than rebuilding it, but never forward userinfo as part of an upstream.
    upstream = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"
    prefix = parts.path.rstrip("/")
    if not prefix and not parts.query:
        return upstream, ""
    # ``{path}`` re-appends the request path to the prefix. The query goes in the
    # rewrite target with ``{query}`` so the bookmark's own parameters are kept
    # alongside whatever the client sent; Caddy drops the dangling ``&``. A
    # fragment is never sent to a server, so it is deliberately ignored.
    target = f"{prefix}{{path}}"
    if parts.query:
        target += f"?{parts.query}&{{query}}"
    return upstream, f"{indent}rewrite * {target}\n"


def _bookmark(route: Route, indent: str = "\t") -> str:
    """Render a bookmark: a redirect by default, a reverse proxy when asked.

    A proxied bookmark keeps the ``.vc.localhost`` name in the address bar, which
    matters for upstreams whose own hostname is not resolvable from the browser
    (a Tailscale node, a device on another VLAN).
    """
    target = str(route.url).rstrip("/")
    if not route.proxy:
        return f"{indent}redir {target}{{uri}} 307\n"

    upstream, rewrite = _proxy_target(route, indent)
    lines = [rewrite, f"{indent}reverse_proxy {upstream} {{\n"]
    # Present the upstream with its own identity: many appliances validate the
    # Origin and Referer they are given and return 400 for anything else. Host is
    # already rewritten to the upstream address by reverse_proxy's own default.
    lines.append(f"{indent}\theader_up -Origin\n")
    lines.append(f"{indent}\theader_up -Referer\n")
    # Home Assistant and similar reject requests carrying X-Forwarded-For from an
    # untrusted proxy; we are a local dev front end, so drop it.
    lines.append(f"{indent}\theader_up -X-Forwarded-For\n")
    # A Domain= on a cookie set for the upstream host will not match the
    # .vc.localhost name the browser is actually on, so the cookie would be dropped.
    lines.append(f"{indent}\theader_down Set-Cookie (?i);\\s*domain=[^;]* ''\n")
    if route.insecure_skip_verify:
        lines.append(f"{indent}\ttransport http {{\n")
        lines.append(f"{indent}\t\ttls_insecure_skip_verify\n")
        lines.append(f"{indent}\t}}\n")
    lines.append(f"{indent}}}\n")
    return "".join(lines)


def _site(route: Route) -> str:
    host = route.hostname
    body = _bookmark(route) if route.type is RouteType.BOOKMARK else _reverse_proxy(route)
    return f"https://{host}, http://{host} {{\n{_bind_line()}{body}}}\n"


def render(data: RegistryData, dashboard_port: int | None = None) -> str:
    """Render the full Caddyfile for a registry snapshot.

    Args:
        data: The registry to render.
        dashboard_port: Local port of the running dashboard. When ``None`` the
            catch-all serves a static message instead, so an unreachable dashboard
            degrades to a plain page rather than a proxy error.

    Returns:
        A canonically formatted Caddyfile, ready for ``caddy validate``.
    """
    ask = f"http://{_LOOPBACK}:{paths.CADDY_ASK_PORT}/check"
    out = [
        "# Generated by vibe-caddy. Edits are overwritten on the next reload.\n",
        "{\n",
        f"\tadmin {paths.CADDY_ADMIN}\n",
        "\t# Issue every certificate from Caddy's own CA; nothing here is public.\n",
        "\tlocal_certs\n",
        "\ton_demand_tls {\n",
        f"\t\task {ask}\n",
        "\t}\n",
        "\tlog {\n",
        f"\t\toutput file {paths.caddy_log()}\n",
        "\t\tlevel INFO\n",
        "\t}\n",
        "}\n\n",
        "# Gate for on-demand certificates. Loopback-only, and reached only by\n",
        f"# Caddy itself. Admits any name under .{paths.TLD} and refuses the rest.\n",
        "# A CEL expression, not a `query` matcher: that matcher compares values\n",
        f'# exactly and treats `*` as "any value", so a `*.{paths.TLD}` pattern there\n',
        "# silently matches nothing and every certificate request is refused.\n",
        f"http://{_LOOPBACK}:{paths.CADDY_ASK_PORT} {{\n",
        f"\t@allowed expression `{{query.domain}}.endsWith('.{paths.TLD}')`\n",
        "\trespond @allowed 200\n",
        "\trespond 403\n",
        "}\n\n",
    ]

    for name in sorted(data.routes):
        out.append(_site(data.routes[name]))
        out.append("\n")

    out.append("# Anything not registered above: the dashboard's unknown-name page.\n")
    out.append("https://, http:// {\n")
    out.append(_bind_line())
    out.append("\ttls {\n\t\ton_demand\n\t}\n")
    if dashboard_port is not None:
        # Refuse other namespaces over HTTP and when an old certificate is cached.
        out.append(f"\t@vibe expression `{{host}}.endsWith('.{paths.TLD}')`\n")
        out.append(f"\treverse_proxy @vibe {_LOOPBACK}:{dashboard_port}\n")
        out.append('\trespond "No vibe-caddy route for {host}." 404\n')
    else:
        out.append('\trespond "No vibe-caddy route for {host}." 404\n')
    out.append("}\n")
    return "".join(out)


@contextmanager
def _publication_lock() -> Generator[None]:
    """Hold the cross-process lock that serializes Caddyfile publication.

    ``registry.transaction()`` releases its lock before anything is published, so
    two overlapping mutations could otherwise write and load their snapshots in
    the opposite order from which they committed, leaving the older one live. A
    separate lock is used so a slow ``caddy reload`` never blocks registry reads
    or commits; it is a distinct open file description each time, so it also
    serializes threads within one process.
    """
    paths.ensure_dirs()
    with (paths.state_dir() / "caddyfile.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _publish(content: str) -> None:
    """Atomically replace the Caddyfile with ``content`` once Caddy accepts it.

    The candidate is validated in a sibling temp file first, so a route that
    renders an invalid config can never overwrite the last known-good file --
    which is also what Caddy would be restarted from, and a bad one there would
    stop all routing. ``os.replace`` keeps readers from seeing a partial file.
    Callers must hold :func:`_publication_lock`.

    Raises:
        VibeError: if Caddy rejects the candidate; the existing file is untouched.
    """
    target = paths.caddyfile()
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".Caddyfile-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Without the binary there is nothing to check against, and nothing
        # could load the file either, so fall through to a plain atomic write.
        if shutil.which("caddy"):
            validate(str(tmp))
        tmp.chmod(0o644)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write(data: RegistryData, dashboard_port: int | None = None) -> str:
    """Render ``data`` and publish it as the Caddyfile under ``XDG_STATE_HOME``.

    Args:
        data: The registry to render. Unlike :func:`reload`, the explicit
            snapshot is honoured: this is for callers (setup) that deliberately
            write a given state.
        dashboard_port: Local port of the dashboard for the catch-all block.

    Returns:
        The content written.

    Raises:
        VibeError: if the rendered configuration is invalid; the previous
            Caddyfile is left in place.
    """
    content = render(data, dashboard_port)
    with _publication_lock():
        _publish(content)
    return content


def dashboard_port_of(data: RegistryData) -> int | None:
    route = data.routes.get(paths.DASHBOARD_ROUTE)
    return route.port if route else None


def hostnames(data: RegistryData) -> Iterable[str]:
    return (route.hostname for route in data.routes.values())


# --------------------------------------------------------------- admin API


def _admin(path: str) -> str:
    return f"http://{paths.CADDY_ADMIN}{path}"


def is_running(timeout: float = 2.0) -> bool:
    """True if something answers Caddy's admin API."""
    try:
        httpx2.get(_admin("/config/"), timeout=timeout)
    except httpx2.HTTPError:
        return False
    return True


def running_config(timeout: float = 2.0) -> dict | None:
    """Return the configuration the live Caddy is serving, or None."""
    try:
        response = httpx2.get(_admin("/config/"), timeout=timeout)
        response.raise_for_status()
    except httpx2.HTTPError:
        return None
    body = response.json()
    return body if isinstance(body, dict) else None


def is_ours(timeout: float = 2.0) -> bool:
    """True if the Caddy on the admin port is the one vibe-caddy configured.

    A developer machine often has another Caddy running already -- a stray
    ``caddy run``, or one in a container with the admin port published. Pushing our
    configuration into it would either fail, because it cannot bind 443
    unprivileged, or silently replace someone else's running config. Every config
    we generate contains the loopback ``ask`` listener, so its presence is a
    reliable signature of our own instance.
    """
    config = running_config(timeout)
    if config is None:
        return False
    return f":{paths.CADDY_ASK_PORT}" in json.dumps(config)


def foreign_instance() -> str | None:
    """Describe a Caddy on the admin port that vibe-caddy did not configure.

    Returns:
        A short description of what holds the port, or None when nothing is
        listening or the listener is ours.
    """
    if not is_running() or is_ours():
        return None
    admin_port = int(paths.CADDY_ADMIN.rsplit(":", 1)[1])
    return ports.describe_holder(admin_port) or "another Caddy instance"


def validate(path: str | None = None) -> None:
    """Check a Caddyfile parses and adapts.

    Raises:
        VibeError: quoting Caddy's own complaint, which names the offending line.
    """
    target = path or str(paths.caddyfile())
    result = subprocess.run(
        ["caddy", "validate", "--adapter", "caddyfile", "--config", target],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=caddy_env(),
    )
    if result.returncode != 0:
        raise VibeError(f"generated Caddyfile is invalid: {clean_error(result.stderr)}")


def reload(*, require_running: bool = True) -> None:
    """Regenerate the Caddyfile from the current registry and ask Caddy to load it.

    ``caddy reload`` adapts the Caddyfile and POSTs it to the admin API; Caddy
    swaps the config atomically and keeps serving the old one if the new one fails
    to load, so a bad route cannot take the whole proxy down.

    There is deliberately no way to pass a snapshot. A caller's snapshot can be
    older than a commit that landed while it waited for the lock, and publishing
    it is exactly the race the lock exists to close. The registry on disk is a
    superset of every committed change, so a caller loses nothing by it being
    re-read here.

    Args:
        require_running: Raise if Caddy is absent or is not ours. Callers that
            have already committed a registry change pass False, because the
            change is real whether or not it is being served yet.

    Raises:
        SetupRequired: if Caddy is not running, or is not ours, and
            ``require_running`` is set.
        VibeError: if Caddy rejects the configuration.
    """
    from . import registry

    with _publication_lock():
        current = registry.load()
        _publish(render(current, dashboard_port_of(current)))
        _load_into_caddy(require_running=require_running)


def _load_into_caddy(*, require_running: bool) -> None:
    """Ask the live Caddy to load the published Caddyfile.

    Runs inside the publication lock, so the file cannot change between being
    written and being loaded.
    """
    if not is_running():
        if require_running:
            raise SetupRequired(
                "Caddy is not running",
                hint="start it with: sudo vibe-caddy setup",
            )
        return

    if not is_ours():
        # Someone else's Caddy holds the admin port. The registry is already
        # written, so the route exists; it simply is not being served yet.
        if require_running:
            raise SetupRequired(
                f"the Caddy on {paths.CADDY_ADMIN} was not started by vibe-caddy",
                hint="stop it, then: sudo vibe-caddy setup",
            )
        return

    result = subprocess.run(
        ["caddy", "reload", "--config", str(paths.caddyfile()), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=caddy_env(),
    )
    if result.returncode != 0:
        raise VibeError(
            f"Caddy rejected the new configuration: {clean_error(result.stderr)}",
            hint=f"inspect it: {paths.caddyfile()}",
        )


def clean_error(stderr: str) -> str:
    """Pull the message out of Caddy's structured log output.

    ``caddy`` emits one JSON object per line; printing all of it buries the single
    line that says what actually went wrong.
    """
    messages = []
    for line in stderr.strip().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            messages.append(line.strip())
            continue
        if record.get("level") in ("error", "fatal"):
            messages.append(str(record.get("msg", "")).strip())
    return " ".join(message for message in messages if message) or "no detail given"


def caddy_env() -> dict[str, str]:
    """Environment pinning Caddy's data directory.

    The root LaunchDaemon and any user-run ``caddy`` command must resolve the same
    data directory, and therefore the same CA; otherwise ``caddy trust`` would
    trust a root that never signs anything the browser sees.
    """
    env = dict(os.environ)
    env["XDG_DATA_HOME"] = str(paths.caddy_data_dir())
    env["HOME"] = str(paths.home())
    return env


def version() -> str | None:
    """Return the installed Caddy version, or None if the binary is missing."""
    try:
        result = subprocess.run(
            ["caddy", "version"], capture_output=True, text=True, timeout=10, check=False
        )
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout.strip().splitlines()[0] if result.returncode == 0 else None
