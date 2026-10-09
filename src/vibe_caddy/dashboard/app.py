"""FastAPI application: the JSON API under ``/_api`` plus the server-rendered UI.

The API is unauthenticated and ``cmd`` is arbitrary code execution, so the real
security boundary is the browser: any web page you visit can make your browser POST to
``http://127.0.0.1:<port>``. :func:`create_app` therefore installs a guard that
refuses state-changing requests which a browser marks as cross-origin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import hashlib

import jinja2
import uvicorn
from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, paths, service
from ..errors import Conflict, NotFound, SetupRequired, VibeError
from ..models import Port

_HERE = Path(__file__).parent

#: Origins that are always the user's own machine.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Fetched by the page itself; scripts and styles must be same-origin files, which
#: also means a route-supplied value that slips past escaping cannot run inline script.
_CSP = (
    "default-src 'self'; img-src 'self' http: https: data:; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)

_STATUS_FOR: tuple[tuple[type[VibeError], int], ...] = (
    (NotFound, 404),
    (Conflict, 409),
    (SetupRequired, 503),
)


# ------------------------------------------------------------------ request models


class RouteCreate(BaseModel):
    name: str
    port: Port | None = None
    cmd: str | None = None
    dir: str | None = None
    url: str | None = None
    proxy: bool = False
    insecure_skip_verify: bool = False
    icon: str | None = None
    autostart: bool = False
    ws_origin_rewrite: bool = True


class RoutePatch(BaseModel):
    """Every field optional; only the ones the client sent are applied."""

    port: Port | None = None
    cmd: str | None = None
    dir: str | None = None
    url: str | None = None
    proxy: bool | None = None
    insecure_skip_verify: bool | None = None
    icon: str | None = None
    autostart: bool | None = None
    ws_origin_rewrite: bool | None = None


class PreferencesPatch(BaseModel):
    view: Literal["list", "grid"] = Field(...)


# -------------------------------------------------------------------------- helpers


def route_json(status: service.RouteStatus) -> dict[str, Any]:
    """Flatten a route and its live state into the shape the UI consumes."""
    body = status.route.model_dump(mode="json")
    body.update(
        state=status.state,
        pid=status.pid,
        listening=status.listening,
        href=status.route.href,
        hostname=status.route.hostname,
        live=is_live(status.state),
        # Resolved server-side: the client should not have to know which
        # framework logos happen to be vendored.
        framework_icon=framework_icon(status.route.framework),
    )
    return body


ICON_DIR = Path(__file__).resolve().parent / "static" / "icons"


#: Sent on every response. See the note where the static files are mounted.
NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"


#: States that mean the route is serving, or about to be. Everything else --
#: stopped, down, crashed -- is something you would have to act on.
LIVE_STATES = frozenset({"ready", "up", "starting"})


def is_live(state: str) -> bool:
    return state in LIVE_STATES


def asset(path: str) -> str:
    """Return a static URL carrying a hash of the file's current contents.

    This complements the no-store headers rather than duplicating them. A
    browser that cached an asset under the old, header-less responses will not
    re-request that URL at all, so no header on the new response can reach it;
    changing the URL is the only thing that does.
    """
    target = Path(__file__).resolve().parent / path.lstrip("/")
    try:
        digest = hashlib.blake2b(target.read_bytes(), digest_size=6).hexdigest()
    except OSError:
        return f"/{path.lstrip('/')}"
    return f"/{path.lstrip('/')}?v={digest}"


def framework_icon(name: str | None) -> str | None:
    """Return the vendored devicon path for a framework, or None.

    The file is checked rather than assumed: a preset added without running
    ``scripts/vendor_icons.py`` should fall back to the initial, not render a
    broken image.
    """
    if not name:
        return None
    if not (ICON_DIR / f"{name}.svg").is_file():
        return None
    return f"/static/icons/{name}.svg"


def is_image_url(value: str | None) -> bool:
    """True when an icon is an http(s) URL to render as ``<img>`` rather than text."""
    if not value:
        return False
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _host_of(raw: str) -> str | None:
    """Extract the lowercase hostname from a ``Host`` header or an ``Origin`` value."""
    try:
        return urlsplit(raw if "//" in raw else f"//{raw}").hostname
    except ValueError:
        return None


def _registered_hosts(*, include_bookmarks: bool) -> set[str]:
    try:
        return service.route_hostnames(include_bookmarks=include_bookmarks)
    except VibeError:
        # A corrupt registry must not turn the guard into an error page; fall back to
        # trusting nothing beyond loopback.
        return set()


def _origin_trusted(origin: str) -> bool:
    host = _host_of(origin)
    if host is None:
        return False  # "null" origins (sandboxed iframes, file://) are never ours
    if host in _LOOPBACK_HOSTS or host == paths.hostname(paths.DASHBOARD_ROUTE):
        return True
    # Bookmarks are excluded on purpose: they point at an upstream we merely proxy,
    # and that upstream must not be able to drive this API from the user's browser.
    return host in _registered_hosts(include_bookmarks=False)


def _error(status: int, message: str, hint: str | None = None) -> JSONResponse:
    return JSONResponse({"error": message, "hint": hint}, status_code=status)


# ---------------------------------------------------------------------------- app


def create_app() -> FastAPI:
    """Build the dashboard application.

    Returns:
        A FastAPI app with the JSON API, the HTML UI, and the CSRF guard installed.
    """
    app = FastAPI(title="vibe-caddy", version=__version__, docs_url=None, redoc_url=None)
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_HERE / "templates"),
        autoescape=jinja2.select_autoescape(["html"]),
    )
    dashboard_host = paths.hostname(paths.DASHBOARD_ROUTE)

    def page(template: str, status: int = 200, **context: Any) -> HTMLResponse:
        response = HTMLResponse(
            env.get_template(template).render(
                is_image_url=is_image_url, framework_icon=framework_icon, asset=asset, **context
            ),
            status_code=status,
        )
        # The page carries the hashed asset URLs, so it must never be a stale
        # copy itself or it would keep pointing at the previous build.
        response.headers["Cache-Control"] = NO_STORE
        return response

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = _host_of(request.headers.get("host", ""))

        # Caddy sends every unregistered *.vc.localhost name to us as a catch-all.
        if (
            host is not None
            and host.endswith(f".{paths.TLD}")
            and host != dashboard_host
            and host not in _registered_hosts(include_bookmarks=True)
            and not request.url.path.startswith("/static/")
        ):
            name = host.removesuffix(f".{paths.TLD}")
            routes = [route_json(s) for s in service.statuses()]
            return page("unknown.html", 404, host=host, name=name, routes=routes)

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site":
                return _error(403, "cross-site request refused")
            # A missing Origin is how curl and the CLI talk to us; browsers always send
            # one on cross-origin writes, so only a present-but-foreign Origin is refused.
            if origin is not None and not _origin_trusted(origin):
                return _error(403, "request origin is not allowed")

        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    @app.exception_handler(VibeError)
    async def vibe_error(_: Request, exc: VibeError) -> JSONResponse:
        status = next((code for kind, code in _STATUS_FOR if isinstance(exc, kind)), 400)
        return _error(status, str(exc), exc.hint)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError) -> JSONResponse:
        # InvalidName and pydantic's ValidationError from the service layer are the
        # caller's mistake, not a server fault.
        return _error(400, str(exc))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ())[1:])
        return _error(422, f"{where}: {first.get('msg', 'invalid request')}".strip(": "))

    # Plain ``def`` handlers below: they shell out (launchctl, caddy) and FastAPI runs
    # them in a worker thread, keeping the event loop free for other requests.

    @app.get("/_api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "routes": service.route_count(), "version": __version__}

    @app.get("/_api/routes")
    def list_routes() -> list[dict[str, Any]]:
        return [route_json(s) for s in service.statuses()]

    @app.post("/_api/routes", status_code=201)
    def create_route(body: RouteCreate) -> dict[str, Any]:
        route = service.register(
            body.name,
            port=body.port,
            cmd=body.cmd or None,
            directory=Path(body.dir) if body.dir else None,
            url=body.url or None,
            proxy=body.proxy,
            insecure_skip_verify=body.insecure_skip_verify,
            icon=body.icon or None,
            autostart=body.autostart,
            ws_origin_rewrite=body.ws_origin_rewrite,
        )
        return route_json(service.status_of(route))

    @app.put("/_api/routes/{name}")
    def update_route(name: str, body: RoutePatch) -> dict[str, Any]:
        route = service.update(name, **body.model_dump(exclude_unset=True))
        return route_json(service.status_of(route))

    @app.delete("/_api/routes/{name}")
    def delete_route(name: str) -> dict[str, Any]:
        route = service.deregister(name)
        return {"deleted": route.name}

    def lifecycle(action: Callable[[str], Any]) -> Callable[[str], dict[str, Any]]:
        def handler(name: str) -> dict[str, Any]:
            return route_json(service.status_of(action(name)))

        return handler

    for verb in (service.start, service.stop, service.restart):
        app.post(f"/_api/routes/{{name}}/{verb.__name__}")(lifecycle(verb))

    @app.get("/_api/routes/{name}/log")
    def route_log(name: str, lines: int = Query(200, ge=1, le=5000)) -> dict[str, str]:
        # registered_only: 404 for unknown names rather than an empty log
        log = service.read_log(name, lines, registered_only=True)
        return {"log": log.text or ""}

    @app.put("/_api/preferences")
    def set_preferences(body: PreferencesPatch) -> dict[str, str]:
        return {"view": service.set_view(body.view)}

    # HEAD as well as GET: probes and link checkers use it, and Starlette does
    # not derive one from the other.
    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return page(
            "index.html",
            routes=[route_json(s) for s in service.statuses()],
            is_live=is_live,
            view=service.get_view(),
            version=__version__,
        )

    # Caching is disabled outright. This is a local dashboard served over
    # loopback where every asset is a few kilobytes, so there is nothing to gain
    # from a cache and a great deal to lose: a stale stylesheet or script after
    # an upgrade looks like a broken dashboard rather than a caching artefact.
    class UncachedStatic(StaticFiles):
        def file_response(self, *args: Any, **kwargs: Any) -> Response:
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = NO_STORE
            return response

    app.mount("/static", UncachedStatic(directory=_HERE / "static"), name="static")
    return app


def serve(host: str, port: int) -> None:
    """Run the dashboard under uvicorn until interrupted.

    Args:
        host: Interface to bind. Should be loopback; the API has no authentication.
        port: TCP port to listen on.
    """
    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level="warning",
        # Without a bound, uvicorn's graceful shutdown waits for open keep-alive
        # connections to close, and a dashboard being polled every three seconds
        # always has one. That held the port for several seconds after `stop`,
        # so an immediate `start` reported the dying process as a port conflict.
        timeout_graceful_shutdown=2,
    )
