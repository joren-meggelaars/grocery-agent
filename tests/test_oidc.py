import base64
import json
import time

import pytest
from pydantic import SecretStr

from grocery.config import Settings
from grocery.security.oidc import Oidc, OidcDenied, OidcError, enabled


def jwt(claims: dict) -> str:
    def part(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def settings(**kw) -> Settings:
    base = dict(
        oidc_issuer="https://auth.example.nl/application/o/grocery-agent/", oidc_client_secret=SecretStr("s3cret"),
        oidc_redirect_uris="https://ga.example.nl/login/oidc/callback", allowed_hosts=["x"], cookie_secure=True,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


class FakeAuthentik:
    """Answers discovery and the token endpoint; issues a valid id_token unless told otherwise."""

    def __init__(self, claims=None, token_status=200, discovery_status=200, issuer=None):
        self.claims, self.token_status, self.discovery_status = claims, token_status, discovery_status
        self.issuer = issuer or "https://auth.example.nl/application/o/grocery-agent/"
        self.calls = []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, data))
        if url.endswith("/.well-known/openid-configuration"):
            if self.discovery_status != 200:
                return self.discovery_status, b""
            return 200, json.dumps({
                "issuer": self.issuer, "authorization_endpoint": "https://auth.example.nl/authorize",
                "token_endpoint": "https://auth.example.nl/token",
            }).encode()
        if url == "https://auth.example.nl/token":
            if self.token_status != 200:
                return self.token_status, b"nope"
            claims = self.claims if self.claims is not None else {
                "iss": self.issuer, "aud": "grocery-agent", "sub": "u-1", "exp": time.time() + 300,
                "nonce": data["code_verifier"] and "PLACEHOLDER",  # replaced per-test via wrap()
                "groups": ["grocery-agent-admin"],
            }
            return 200, json.dumps({"id_token": jwt(claims)}).encode()
        raise AssertionError(f"unexpected call to {url}")


def do_flow(o: Oidc, ak: FakeAuthentik, host="ga.example.nl", extra_claims=None):
    """start() then finish() with the right nonce/state wired through, like a browser round trip."""
    target, cookie = o.start(host, "/receipts")
    nonce = target.split("nonce=")[1].split("&")[0]
    state = target.split("state=")[1].split("&")[0]
    ak.claims = {
        "iss": ak.issuer, "aud": "grocery-agent", "sub": "u-1", "exp": time.time() + 300, "nonce": nonce,
        "groups": ["grocery-agent-admin"],
        **(extra_claims or {}),
    }
    return o.finish("the-code", state, cookie)


# --- enabled() and config validation are covered in test_config.py -----------------------------

def test_enabled_follows_the_issuer():
    assert enabled(settings()) is True
    assert enabled(settings(oidc_issuer="")) is False


# --- a full sign-in round trip -------------------------------------------------------------------

def test_a_full_sign_in_succeeds_and_returns_the_subject_and_destination():
    ak = FakeAuthentik()
    o = Oidc(settings(), fetch=ak)
    sub, next_path = do_flow(o, ak)
    assert (sub, next_path) == ("u-1", "/receipts")


def test_the_authorize_url_carries_pkce_and_the_registered_redirect_uri():
    ak = FakeAuthentik()
    o = Oidc(settings(), fetch=ak)
    target, _ = o.start("ga.example.nl", "/receipts")
    assert target.startswith("https://auth.example.nl/authorize?")
    assert "code_challenge=" in target and "code_challenge_method=S256" in target
    assert "redirect_uri=https%3A%2F%2Fga.example.nl%2Flogin%2Foidc%2Fcallback" in target


def test_the_redirect_uri_matches_the_host_the_browser_used():
    o = Oidc(settings(oidc_redirect_uris="https://a.example/login/oidc/callback,https://b.example/login/oidc/callback"))
    assert o.redirect_uri_for("b.example") == "https://b.example/login/oidc/callback"
    assert o.redirect_uri_for("unknown.example") == "https://a.example/login/oidc/callback"  # falls back to the first


# --- what finish() rejects ----------------------------------------------------------------------

def test_a_missing_or_tampered_pending_cookie_is_rejected():
    o = Oidc(settings(), fetch=FakeAuthentik())
    with pytest.raises(OidcError, match="no sign-in in progress"):
        o.finish("code", "state", None)
    with pytest.raises(OidcError, match="no sign-in in progress"):
        o.finish("code", "state", "garbage")


def test_a_cookie_from_another_client_secret_is_rejected():
    ak = FakeAuthentik()
    _, cookie = Oidc(settings(oidc_client_secret=SecretStr("s3cret")), fetch=ak).start("ga.example.nl", "/x")
    other = Oidc(settings(oidc_client_secret=SecretStr("different")), fetch=ak)
    with pytest.raises(OidcError, match="no sign-in in progress"):
        other.finish("code", "state", cookie)


def test_an_expired_pending_cookie_is_rejected():
    now = [1000.0]
    o = Oidc(settings(), fetch=FakeAuthentik(), clock=lambda: now[0])
    target, cookie = o.start("ga.example.nl", "/x")
    state = target.split("state=")[1].split("&")[0]
    now[0] += 601
    with pytest.raises(OidcError, match="no sign-in in progress"):
        o.finish("code", state, cookie)


def test_a_state_mismatch_is_rejected():
    ak = FakeAuthentik()
    o = Oidc(settings(), fetch=ak)
    _, cookie = o.start("ga.example.nl", "/x")
    with pytest.raises(OidcError, match="state mismatch"):
        o.finish("code", "wrong-state", cookie)
    with pytest.raises(OidcError, match="state mismatch"):
        o.finish("", "", cookie)


def test_a_wrong_issuer_in_the_id_token_is_rejected():
    ak = FakeAuthentik()
    o = Oidc(settings(), fetch=ak)
    with pytest.raises(OidcError, match="wrong issuer"):
        do_flow(o, ak, extra_claims={"iss": "https://evil.example/"})


def test_a_token_for_a_different_client_is_rejected():
    ak = FakeAuthentik()
    o = Oidc(settings(), fetch=ak)
    with pytest.raises(OidcError, match="not for this client"):
        do_flow(o, ak, extra_claims={"aud": "someone-else"})
    with pytest.raises(OidcError, match="not for this client"):
        do_flow(o, ak, extra_claims={"aud": ["a", "b"]})


def test_an_expired_id_token_is_rejected():
    ak = FakeAuthentik()
    o = Oidc(settings(), fetch=ak)
    with pytest.raises(OidcError, match="expired"):
        do_flow(o, ak, extra_claims={"exp": time.time() - 600})


def test_a_nonce_mismatch_is_rejected():
    ak = FakeAuthentik()
    o = Oidc(settings(), fetch=ak)
    with pytest.raises(OidcError, match="nonce mismatch"):
        do_flow(o, ak, extra_claims={"nonce": "not-the-right-one"})


def test_a_missing_sub_is_rejected():
    ak = FakeAuthentik()
    o = Oidc(settings(), fetch=ak)
    with pytest.raises(OidcError, match="no sub"):
        do_flow(o, ak, extra_claims={"sub": ""})


def test_someone_outside_the_admin_group_is_denied():
    ak = FakeAuthentik()
    o = Oidc(settings(), fetch=ak)
    with pytest.raises(OidcDenied):
        do_flow(o, ak, extra_claims={"groups": ["some-other-app-admin"]})
    with pytest.raises(OidcDenied):
        do_flow(o, ak, extra_claims={"groups": "not-a-list"})


def test_a_custom_admin_group_is_honoured():
    ak = FakeAuthentik()
    o = Oidc(settings(oidc_admin_group="custom-group"), fetch=ak)
    with pytest.raises(OidcDenied):
        do_flow(o, ak)  # only in the default group, not "custom-group"
    sub, _ = do_flow(o, ak, extra_claims={"groups": ["custom-group"]})
    assert sub == "u-1"


def test_a_bad_id_token_shape_is_rejected():
    class BadToken(FakeAuthentik):
        def __call__(self, method, url, headers, data):
            if url == "https://auth.example.nl/token":
                return 200, b'{"id_token": "not-a-jwt"}'
            return super().__call__(method, url, headers, data)

    bad = BadToken()
    o = Oidc(settings(), fetch=bad)
    target, cookie = o.start("ga.example.nl", "/x")
    state = target.split("state=")[1].split("&")[0]
    with pytest.raises(OidcError, match="unusable token response"):
        o.finish("code", state, cookie)


def test_the_token_endpoint_is_asked_with_pkce_and_the_client_secret():
    ak = FakeAuthentik()
    o = Oidc(settings(oidc_client_secret=SecretStr("s3cret")), fetch=ak)
    do_flow(o, ak)
    method, url, headers, data = ak.calls[-1]
    assert (method, url) == ("POST", "https://auth.example.nl/token")
    assert data["client_secret"] == "s3cret" and data["grant_type"] == "authorization_code"
    assert len(data["code_verifier"]) >= 40


# --- discovery -------------------------------------------------------------------------------

def test_discovery_is_cached_across_calls():
    ak = FakeAuthentik()
    o = Oidc(settings(), fetch=ak)
    do_flow(o, ak)
    calls_after_first = len([c for c in ak.calls if "openid-configuration" in c[1]])
    do_flow(o, ak)
    calls_after_second = len([c for c in ak.calls if "openid-configuration" in c[1]])
    assert calls_after_first == calls_after_second == 1


def test_a_discovery_error_status_is_reported():
    o = Oidc(settings(), fetch=FakeAuthentik(discovery_status=503))
    with pytest.raises(OidcError, match="503"):
        o.start("ga.example.nl", "/x")


def test_an_issuer_mismatch_at_discovery_is_reported():
    o = Oidc(settings(), fetch=FakeAuthentik(issuer="https://someone-else.example/"))
    with pytest.raises(OidcError, match="issuer mismatch"):
        o.start("ga.example.nl", "/x")


def test_discovery_missing_endpoints_is_reported():
    class NoEndpoints(FakeAuthentik):
        def __call__(self, method, url, headers, data):
            if url.endswith("openid-configuration"):
                return 200, json.dumps({"issuer": self.issuer}).encode()
            return super().__call__(method, url, headers, data)

    o = Oidc(settings(), fetch=NoEndpoints())
    with pytest.raises(OidcError, match="authorization_endpoint"):
        o.start("ga.example.nl", "/x")


def test_a_token_endpoint_error_status_is_reported():
    ak = FakeAuthentik(token_status=400)
    o = Oidc(settings(), fetch=ak)
    target, cookie = o.start("ga.example.nl", "/x")
    state = target.split("state=")[1].split("&")[0]
    with pytest.raises(OidcError, match="400"):
        o.finish("code", state, cookie)


def test_a_network_error_becomes_an_oidc_error():
    def down(method, url, headers, data):
        raise OSError("unreachable")

    o = Oidc(settings(), fetch=down)
    with pytest.raises(OidcError, match="cannot reach"):
        o.start("ga.example.nl", "/x")


# --- OIDC_INTERNAL_URL rewrites the host but keeps the public Host header -----------------------

def test_internal_url_rewrites_the_connection_but_forwards_the_public_host():
    ak = FakeAuthentik()

    def fetch(method, url, headers, data):
        assert url.startswith("http://authentik:9000/")
        assert headers["Host"] == "auth.example.nl" and headers["X-Forwarded-Proto"] == "https"
        return ak(method, url.replace("http://authentik:9000", "https://auth.example.nl"), headers, data)

    o = Oidc(settings(oidc_internal_url="http://authentik:9000"), fetch=fetch)
    sub, _ = do_flow(o, ak)
    assert sub == "u-1"
