from sqlalchemy import select

from grocery.db.models import AuthSession
from grocery.security import ratelimit
from tests.conftest import PASSWORD, csrf_of, login


def test_anonymous_html_request_redirects_to_login_with_next(client):
    resp = client.get("/account", headers={"accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Faccount"


def test_login_page_renders(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'name="password"' in resp.text


def test_successful_login_sets_a_hardened_cookie(client):
    resp = login(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("__Host-session=")
    lowered = cookie.lower()
    for flag in ("httponly", "secure", "samesite=strict", "path=/"):
        assert flag in lowered
    assert "domain=" not in lowered
    assert "joren" in client.get("/").text


def test_wrong_password_and_unknown_user_look_identical(client):
    wrong = login(client, password="not the right password")
    unknown = login(client, username="nobody")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.text == unknown.text
    assert "__Host-session" not in wrong.headers.get("set-cookie", "")


def test_lockout_blocks_even_the_correct_password(client):
    for _ in range(ratelimit.FREE_FAILURES):
        assert login(client, password="wrong wrong wrong").status_code == 401
    resp = login(client)
    assert resp.status_code == 429
    assert "set-cookie" not in resp.headers
    assert client.get("/", headers={"accept": "application/json"}).status_code == 401


def test_open_redirects_are_neutralised(client):
    for evil in ("//evil.example/x", "https://evil.example", "/\\evil.example"):
        resp = login(client, next_url=evil)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/", evil
        client.cookies.clear()


def test_next_is_honoured_for_local_paths(client):
    assert login(client, next_url="/account").headers["location"] == "/account"


def test_logout_needs_the_csrf_token(client):
    login(client)
    assert client.post("/logout").status_code == 403
    assert client.get("/").status_code == 200  # still signed in


def test_logout_with_token_ends_the_session(client, db):
    login(client)
    token = csrf_of(client)
    resp = client.post("/logout", data={"csrf_token": token})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert db.scalar(select(AuthSession)) is None
    assert client.get("/", headers={"accept": "application/json"}).status_code == 401


def test_csrf_token_is_also_accepted_as_a_header(client):
    login(client)
    token = csrf_of(client)
    assert client.post("/logout", headers={"X-CSRF-Token": token}).status_code == 303


def test_wrong_csrf_token_is_rejected(client):
    login(client)
    assert client.post("/logout", data={"csrf_token": "x" * 43}).status_code == 403


def test_a_stolen_csrf_token_from_another_session_does_not_work(client, make_client):
    login(client)
    other = make_client(app=client.app)
    login(other)
    assert other.post("/logout", data={"csrf_token": csrf_of(client)}).status_code == 403


def test_account_page_lists_devices_and_revokes_others(client, make_client, db):
    login(client)
    other = make_client(app=client.app)
    login(other)
    assert len(db.scalars(select(AuthSession)).all()) == 2

    page = client.get("/account")
    assert page.status_code == 200 and "this device" in page.text

    resp = client.post("/account/sessions/revoke-others", data={"csrf_token": csrf_of(client)})
    assert resp.status_code == 303
    assert len(db.scalars(select(AuthSession)).all()) == 1
    assert other.get("/", headers={"accept": "application/json"}).status_code == 401
    assert client.get("/").status_code == 200


def test_login_events_are_recorded(client, db):
    login(client, password="wrong wrong wrong")
    login(client)
    from grocery.db.models import AuthEvent

    kinds = [e.kind for e in db.scalars(select(AuthEvent).order_by(AuthEvent.id))]
    assert kinds == ["login_fail", "login_ok"]


def test_password_survives_round_trip_through_the_form(client):
    assert login(client, password=PASSWORD).status_code == 303


# --- signing in through Authentik --------------------------------------------------------------

import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

from pydantic import SecretStr

from grocery.security import oidc


def jwt(claims: dict) -> str:
    def part(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


ISSUER = "https://auth.example.nl/application/o/grocery-agent/"
REDIRECT = "https://testserver/login/oidc/callback"


def oidc_client(make_client, monkeypatch, group="grocery-agent-admin", **kw):
    """A client with OIDC enabled and a FakeAuthentik installed for _urllib_fetch.

    Returns (client, nonce_box): nonce_box["nonce"] is filled in by sign_in() once it has parsed the
    authorize URL the browser would have been sent to (the browser side of that hop is never actually
    made in these tests, so the fake token endpoint learns the nonce this way instead).
    """
    nonce_box: dict = {}

    def fetch(method, url, headers, data):
        if url.endswith("/.well-known/openid-configuration"):
            return 200, json.dumps({
                "issuer": ISSUER, "authorization_endpoint": "https://auth.example.nl/authorize",
                "token_endpoint": "https://auth.example.nl/token",
            }).encode()
        if url == "https://auth.example.nl/token":
            claims = {
                "iss": ISSUER, "aud": "grocery-agent", "sub": kw.get("sub", "sub-joren"), "exp": time.time() + 300,
                "nonce": nonce_box.get("nonce"), "groups": [group] if group else [],
            }
            return 200, json.dumps({"id_token": jwt(claims)}).encode()
        raise AssertionError(url)

    monkeypatch.setattr(oidc, "_urllib_fetch", fetch)
    client = make_client(
        oidc_issuer=ISSUER, oidc_client_secret=SecretStr("s3cret"), oidc_redirect_uris=REDIRECT,
        allowed_hosts=["testserver"],
    )
    return client, nonce_box


def sign_in(client, nonce_box, next_url="/"):
    """Follows Start -> Authentik's authorize page (never actually called) -> our own callback."""
    start = client.get(f"/login/oidc?next={next_url}")
    assert start.status_code == 303
    query = parse_qs(urlsplit(start.headers["location"]).query)
    nonce_box["nonce"] = query["nonce"][0]
    cookie = start.headers["set-cookie"]
    pending = cookie.split("oidc_pending=", 1)[1].split(";", 1)[0]
    client.cookies.set("oidc_pending", pending)
    return client.get("/login/oidc/callback", params={"code": "the-code", "state": query["state"][0]})


def _oidc_db(client):
    return client.app.state.session_factory()


def test_signing_in_links_to_an_existing_user(make_client, monkeypatch):
    from grocery.cli import link_oidc

    client, box = oidc_client(make_client, monkeypatch)
    with _oidc_db(client) as db:
        link_oidc(db, "joren", "sub-joren")
    resp = sign_in(client, box, "/receipts")
    assert resp.status_code == 303 and resp.headers["location"] == "/receipts"
    assert "joren" in client.get("/").text
    assert "__Host-session=" in resp.headers["set-cookie"] or "session=" in resp.headers["set-cookie"]


def test_the_pending_cookie_is_cleared_after_signing_in(make_client, monkeypatch):
    from grocery.cli import link_oidc

    client, box = oidc_client(make_client, monkeypatch)
    with _oidc_db(client) as db:
        link_oidc(db, "joren", "sub-joren")
    resp = sign_in(client, box)
    assert "oidc_pending=;" in resp.headers["set-cookie"] or 'oidc_pending=""' in resp.headers["set-cookie"]


def test_a_group_member_not_linked_to_a_local_user_is_denied(make_client, monkeypatch):
    client, box = oidc_client(make_client, monkeypatch, sub="unlinked-sub")
    resp = sign_in(client, box)
    assert resp.status_code == 403 and "not linked" in resp.text
    assert "__Host-session=" not in resp.headers.get("set-cookie", "") or "Max-Age=0" in resp.headers["set-cookie"]


def test_someone_outside_the_admin_group_is_denied(make_client, monkeypatch):
    from grocery.cli import link_oidc

    client, box = oidc_client(make_client, monkeypatch, group="some-other-group")
    with _oidc_db(client) as db:
        link_oidc(db, "joren", "sub-joren")
    resp = sign_in(client, box)
    assert resp.status_code == 403 and "not linked" in resp.text


def test_a_deactivated_user_cannot_sign_in_through_oidc(make_client, monkeypatch):
    from sqlalchemy import select

    from grocery.cli import link_oidc
    from grocery.db.models import User

    client, box = oidc_client(make_client, monkeypatch)
    with _oidc_db(client) as db:
        link_oidc(db, "joren", "sub-joren")
        db.get(User, db.scalar(select(User.id))).is_active = False
        db.commit()
    resp = sign_in(client, box)
    assert resp.status_code == 403


def test_cancelling_at_authentik_shows_a_friendly_message(make_client, monkeypatch):
    client, box = oidc_client(make_client, monkeypatch)
    client.get("/login/oidc")  # sets a pending cookie, never used
    resp = client.get("/login/oidc/callback", params={"error": "access_denied"})
    assert resp.status_code == 400 and "cancelled or refused" in resp.text


def test_a_callback_without_a_pending_cookie_fails_cleanly(make_client, monkeypatch):
    client, box = oidc_client(make_client, monkeypatch)
    resp = client.get("/login/oidc/callback", params={"code": "x", "state": "y"})
    assert resp.status_code == 400 and "Sign-in failed" in resp.text


def test_the_login_page_has_no_authentik_button_by_default(client):
    assert "Sign in with Authentik" not in client.get("/login").text


def test_the_login_page_offers_the_authentik_button_when_configured(make_client, monkeypatch):
    client, box = oidc_client(make_client, monkeypatch)
    page = client.get("/login").text
    assert "Sign in with Authentik" in page and 'href="/login/oidc' in page


def test_the_oidc_routes_are_reachable_while_signed_out(make_client, monkeypatch):
    client, box = oidc_client(make_client, monkeypatch)
    assert client.get("/login/oidc").status_code == 303
    assert client.get("/login/oidc/callback").status_code == 400


def test_the_oidc_routes_redirect_to_login_when_oidc_is_off(client):
    assert client.get("/login/oidc").status_code == 303
    assert client.get("/login/oidc").headers["location"] == "/login"


def test_a_denied_sign_in_is_recorded_and_can_trip_the_lockout(make_client, monkeypatch):
    client, box = oidc_client(make_client, monkeypatch, sub="unlinked-sub")
    sign_in(client, box)
    from grocery.db.models import AuthEvent

    with _oidc_db(client) as db:
        events = db.scalars(select(AuthEvent).order_by(AuthEvent.id)).all()
    assert events and events[-1].kind == "login_fail" and "unlinked-sub" in events[-1].username
