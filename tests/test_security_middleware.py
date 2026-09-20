from tests.conftest import login


def test_security_headers_on_every_response(client):
    resp = client.get("/login")
    assert "script-src 'self'" in resp.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "camera=(self)" in resp.headers["permissions-policy"]
    assert "max-age" in resp.headers["strict-transport-security"]
    assert resp.headers["cache-control"] == "no-store"


def test_security_headers_also_on_rejections(client):
    resp = client.get("/login", headers={"Host": "evil.example"})
    assert resp.status_code == 400
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_static_assets_are_revalidated_not_stored(client):
    resp = client.get("/static/css/app.css")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


def test_unknown_host_is_rejected(client):
    assert client.get("/login", headers={"Host": "evil.example"}).status_code == 400


def test_host_port_is_ignored_when_matching(client):
    assert client.get("/login", headers={"Host": "testserver:8443"}).status_code == 200


def test_healthz_is_reachable_with_any_host_and_reports_db(client):
    resp = client.get("/healthz", headers={"Host": "127.0.0.1:8000"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_cross_origin_post_is_rejected(client):
    resp = login(client)  # baseline works
    assert resp.status_code == 303
    client.cookies.clear()
    client.headers["Origin"] = "https://evil.example"
    assert login(client).status_code == 403


def test_cross_site_fetch_metadata_is_rejected(client):
    client.headers["Sec-Fetch-Site"] = "cross-site"
    assert login(client).status_code == 403


def test_post_without_origin_or_fetch_metadata_is_rejected(client):
    del client.headers["Origin"]
    assert login(client).status_code == 403


def test_post_without_origin_is_ok_if_browser_says_same_origin(client):
    del client.headers["Origin"]
    client.headers["Sec-Fetch-Site"] = "same-origin"
    assert login(client, password="wrong wrong wrong").status_code == 401  # got past the check


def test_null_origin_is_rejected(client):
    client.headers["Origin"] = "null"
    assert login(client).status_code == 403


def test_empty_allowed_hosts_falls_back_to_request_host(make_client):
    client = make_client(allowed_hosts=[])
    assert client.get("/login").status_code == 200
    client.headers["Origin"] = "https://evil.example"
    assert login(client).status_code == 403
