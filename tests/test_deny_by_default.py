"""Every route must require a session unless it is on this allowlist."""

import re

from fastapi.routing import APIRoute

from grocery.security.deps import is_public

EXPECTED_PUBLIC = {
    ("/healthz", "GET"),
    ("/login", "GET"),
    ("/login", "POST"),
    ("/sw.js", "GET"),  # service worker and manifest: nothing private in them
    ("/manifest.webmanifest", "GET"),
}


def _flatten(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):  # FastAPI wraps included routers
            yield from _flatten(route.original_router.routes)


def _api_routes(app):
    return list(_flatten(app.routes))


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def test_public_endpoints_are_exactly_the_allowlist(client):
    public = {
        (r.path, method)
        for r in _api_routes(client.app)
        if is_public(r.endpoint)
        for method in r.methods
    }
    assert public == EXPECTED_PUBLIC


def test_protected_routes_reject_anonymous_requests(client):
    checked = 0
    for route in _api_routes(client.app):
        if is_public(route.endpoint):
            continue
        for method in route.methods:
            headers = {"accept": "text/html"} if method == "GET" else {}
            resp = client.request(method, _concrete(route.path), headers=headers)
            expected = 303 if method == "GET" else 401
            assert resp.status_code == expected, f"{method} {route.path} -> {resp.status_code}"
            checked += 1
    assert checked >= 15


def test_anonymous_api_style_get_is_401_not_a_redirect(client):
    assert client.get("/", headers={"accept": "application/json"}).status_code == 401


def test_a_route_added_later_is_protected_without_any_extra_work(client):
    app = client.app

    @app.get("/added-later")
    def added_later():  # no @public, no explicit dependency
        return {"secret": True}

    assert client.get("/added-later", headers={"accept": "application/json"}).status_code == 401


def test_interactive_docs_and_schema_are_not_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
