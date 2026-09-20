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
