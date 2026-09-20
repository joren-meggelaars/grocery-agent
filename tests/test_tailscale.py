from tests.conftest import PASSWORD, login

TS = "Tailscale-User-Login"
OK_LOGIN = "joren@example.com"


def test_off_mode_ignores_the_header_entirely(client):
    resp = client.get("/login", headers={TS: "anyone@example.com"})
    assert resp.status_code == 200
    assert login(client).status_code == 303


def test_require_mode_blocks_requests_without_identity(make_client):
    client = make_client(ts_identity_mode="require")
    assert client.get("/login").status_code == 403
    assert client.get("/static/css/app.css").status_code == 403


def test_require_mode_blocks_forged_header_from_untrusted_peer(make_client):
    client = make_client(ts_identity_mode="require", peer=("10.9.9.9", 1234))
    assert client.get("/login", headers={TS: OK_LOGIN}).status_code == 403


def test_require_mode_blocks_other_tailnet_users(make_client):
    client = make_client(ts_identity_mode="require")
    assert client.get("/login", headers={TS: "someone-else@example.com"}).status_code == 403


def test_require_mode_still_needs_the_password(make_client):
    client = make_client(ts_identity_mode="require", headers={TS: OK_LOGIN})
    assert client.get("/login").status_code == 200
    assert login(client, password="wrong wrong wrong").status_code == 401
    assert login(client, password=PASSWORD).status_code == 303
    assert "joren" in client.get("/").text


def test_require_mode_binds_a_user_to_their_tailscale_login(make_client):
    # A different allow-listed identity must not be able to log in as this user.
    client = make_client(
        ts_identity_mode="require",
        ts_allowed_logins=[OK_LOGIN, "partner@example.com"],
        headers={TS: "partner@example.com"},
    )
    assert login(client, password=PASSWORD).status_code == 401


def test_require_mode_leaves_healthz_open_for_the_healthcheck(make_client):
    client = make_client(ts_identity_mode="require")
    assert client.get("/healthz").status_code == 200


def test_sso_mode_logs_in_with_identity_alone(make_client):
    client = make_client(ts_identity_mode="sso", headers={TS: OK_LOGIN})
    resp = client.get("/login?next=/account")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account"
    assert "__Host-session=" in resp.headers["set-cookie"]
    assert client.get("/account").status_code == 200


def test_sso_mode_without_a_mapped_user_falls_back_to_password(make_client, make_app):
    app = make_app(ts_identity_mode="sso", ts_allowed_logins=[OK_LOGIN, "unmapped@example.com"])
    client = make_client(app=app, headers={TS: "unmapped@example.com"})
    assert client.get("/login").status_code == 200


def test_sso_mode_rejects_forged_header_from_untrusted_peer(make_client):
    client = make_client(ts_identity_mode="sso", peer=("10.9.9.9", 1234), headers={TS: OK_LOGIN})
    assert client.get("/login").status_code == 403


def test_identity_header_is_case_insensitive(make_client):
    client = make_client(ts_identity_mode="require", headers={TS: "  Joren@Example.COM "})
    assert client.get("/login").status_code == 200
