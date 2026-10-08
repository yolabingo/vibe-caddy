"""Dashboard API, CSRF guard and unknown-host page.

``paths.home`` is pointed at a tmp dir and the launchd/network probes are stubbed, so
no test touches the real ``~/.vibe-caddy``, launchd, or a running Caddy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vibe_caddy import __version__, caddy, launchd, paths, ports
from vibe_caddy.dashboard import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(paths, "home", lambda: tmp_path)
    monkeypatch.setattr(caddy, "is_running", lambda: False)
    monkeypatch.setattr(ports, "is_listening", lambda port: False)
    monkeypatch.setattr(launchd, "state", lambda label: launchd.JobState(False))
    return TestClient(create_app(), base_url="http://vibe.localhost")


def test_health(client: TestClient) -> None:
    assert client.get("/_api/health").json() == {
        "status": "ok",
        "routes": 0,
        "version": __version__,
    }


def test_routes_listing_includes_status(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "web", "port": 3100})
    (route,) = client.get("/_api/routes").json()
    assert route["name"] == "web"
    assert route["state"] == "down"
    assert route["hostname"] == "web.localhost"
    assert route["href"] == "https://web.localhost"
    assert route["listening"] is False
    assert route["pid"] is None


def test_create_update_delete_round_trip(client: TestClient) -> None:
    created = client.post("/_api/routes", json={"name": "web", "port": 3100})
    assert created.status_code == 201

    updated = client.put("/_api/routes/web", json={"icon": "W", "port": 3101})
    assert updated.status_code == 200
    assert updated.json()["icon"] == "W"
    assert updated.json()["port"] == 3101

    assert client.delete("/_api/routes/web").json() == {"deleted": "web"}
    assert client.get("/_api/routes").json() == []


def test_unknown_route_is_404(client: TestClient) -> None:
    for response in (
        client.put("/_api/routes/nope", json={"icon": "x"}),
        client.delete("/_api/routes/nope"),
        client.get("/_api/routes/nope/log"),
        client.post("/_api/routes/nope/start"),
    ):
        assert response.status_code == 404
        assert "nope" in response.json()["error"]


def test_duplicate_name_is_409(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "web", "port": 3100})
    response = client.post("/_api/routes", json={"name": "web", "port": 3101})
    assert response.status_code == 409
    assert response.json()["hint"]


def test_invalid_name_is_400(client: TestClient) -> None:
    assert client.post("/_api/routes", json={"name": "Bad_Name!", "port": 3100}).status_code == 400


def test_lifecycle_on_unmanaged_route_is_400(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "web", "port": 3100})
    assert client.post("/_api/routes/web/start").status_code == 400


def test_log_and_preferences(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "web", "port": 3100})
    assert client.get("/_api/routes/web/log").json() == {"log": ""}

    assert client.put("/_api/preferences", json={"view": "list"}).json() == {"view": "list"}
    assert 'data-view="list"' in client.get("/").text
    assert client.put("/_api/preferences", json={"view": "tiles"}).status_code == 422


def test_index_escapes_route_values(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "web", "port": 3100, "icon": "<script>x</script>"})
    html = client.get("/").text
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_image_icon_renders_img(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "web", "port": 3100, "icon": "https://x.test/i.png"})
    assert '<img src="https://x.test/i.png"' in client.get("/").text


# ------------------------------------------------------------------ CSRF guard

WRITE = {"name": "web", "port": 3100}


def test_cross_site_fetch_metadata_rejected(client: TestClient) -> None:
    response = client.post("/_api/routes", json=WRITE, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert client.get("/_api/routes").json() == []


def test_foreign_origin_rejected(client: TestClient) -> None:
    response = client.post("/_api/routes", json=WRITE, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_null_origin_rejected(client: TestClient) -> None:
    assert client.post("/_api/routes", json=WRITE, headers={"Origin": "null"}).status_code == 403


def test_missing_origin_allowed(client: TestClient) -> None:
    assert client.post("/_api/routes", json=WRITE).status_code == 201


@pytest.mark.parametrize(
    "origin",
    [
        "https://vibe.localhost",
        "http://localhost:7999",
        "http://127.0.0.1:7999",
        "http://[::1]:7999",
    ],
)
def test_same_site_origin_allowed(client: TestClient, origin: str) -> None:
    headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin"}
    assert client.post("/_api/routes", json=WRITE, headers=headers).status_code == 201


def test_registered_app_trusted_but_bookmark_is_not(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "app", "port": 3200})
    client.post("/_api/routes", json={"name": "docs", "url": "https://example.com"})

    ok = client.post(
        "/_api/routes",
        json={"name": "x", "port": 3300},
        headers={"Origin": "https://app.localhost"},
    )
    assert ok.status_code == 201

    denied = client.post(
        "/_api/routes",
        json={"name": "y", "port": 3301},
        headers={"Origin": "https://docs.localhost"},
    )
    assert denied.status_code == 403


def test_reads_are_not_guarded(client: TestClient) -> None:
    response = client.get("/_api/health", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200


# ------------------------------------------------------------- unknown hostname


def test_unknown_hostname_page(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "web", "port": 3100})
    response = client.get("/", headers={"Host": "ghost.localhost"})
    assert response.status_code == 404
    assert "ghost.localhost" in response.text
    assert "vibe-caddy register ghost &lt;port&gt;" in response.text
    assert 'href="https://web.localhost"' in response.text


def test_unknown_hostname_escapes_host(client: TestClient) -> None:
    response = client.get("/", headers={"Host": "<b>.localhost"})
    assert response.status_code == 404
    assert "<b>" not in response.text


def test_registered_hostname_is_not_unknown_page(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "web", "port": 3100})
    assert client.get("/", headers={"Host": "web.localhost"}).status_code == 200


# ------------------------------------------------------------------ caching


def test_asset_urls_are_content_hashed(client: TestClient) -> None:
    """A browser holding an old asset will not re-request the same URL."""
    body = client.get("/").text
    assert re.search(r"/static/app\.css\?v=[0-9a-f]{12}", body)
    assert re.search(r"/static/app\.js\?v=[0-9a-f]{12}", body)


def test_asset_hash_tracks_file_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vibe_caddy.dashboard import app as dashboard_app

    before = dashboard_app.asset("static/app.css")
    target = Path(dashboard_app.__file__).resolve().parent / "static" / "app.css"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n/* changed */\n")
        assert dashboard_app.asset("static/app.css") != before
    finally:
        target.write_bytes(original)
    assert dashboard_app.asset("static/app.css") == before


def test_pages_are_not_cached(client: TestClient) -> None:
    assert "no-store" in client.get("/").headers["cache-control"]


def test_static_files_are_not_cached(client: TestClient) -> None:
    response = client.get("/static/app.css")
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]


def test_a_missing_asset_still_yields_a_usable_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hash is an optimisation; an unreadable file must not break the page."""
    from vibe_caddy.dashboard import app as dashboard_app

    assert dashboard_app.asset("static/does-not-exist.css") == "/static/does-not-exist.css"


def test_head_on_the_index_is_allowed(client: TestClient) -> None:
    """Probes and link checkers use HEAD; Starlette does not derive it from GET."""
    response = client.head("/")
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]


# ------------------------------------------------------------------ grouping


@pytest.mark.parametrize(
    ("state", "live"),
    [
        ("ready", True),
        ("up", True),
        ("starting", True),
        ("stopped", False),
        ("down", False),
        ("crashed", False),
    ],
)
def test_live_classification(state: str, live: bool) -> None:
    """Grouping hinges on this, and 'starting' belongs with running, not stopped."""
    from vibe_caddy.dashboard.app import is_live

    assert is_live(state) is live


def test_page_renders_both_group_sections(client: TestClient) -> None:
    body = client.get("/").text
    assert 'id="group-live"' in body
    assert 'id="group-idle"' in body
    # The two lists must have distinct ids or the client reconciles one twice.
    assert 'id="routes"' in body
    assert 'id="routes-idle"' in body


def test_route_payload_carries_the_live_flag(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "a", "port": 3100})
    routes = client.get("/_api/routes").json()
    assert "live" in routes[0]


def test_adding_a_command_through_the_api_promotes_the_route(client: TestClient) -> None:
    """The 'Add command' button is this call; it must yield a startable route."""
    client.post("/_api/routes", json={"name": "a", "port": 3100})
    response = client.put("/_api/routes/a", json={"cmd": "npm run dev", "dir": "/tmp"})
    assert response.status_code == 200, response.text
    assert response.json()["type"] == "managed"


def test_static_routes_offer_the_adopt_action(client: TestClient) -> None:
    client.post("/_api/routes", json={"name": "a", "port": 3100})
    body = client.get("/").text
    assert 'data-action="adopt"' in body
