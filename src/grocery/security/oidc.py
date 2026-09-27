"""Sign in through Authentik (OpenID Connect): authorization-code flow with PKCE.

Optional: with OIDC_ISSUER empty the login page only offers username/password (grocery.security.passwords).
With it set, the login page also gets a "Sign in with Authentik" button; the password login stays as the
emergency way in when Authentik is down.

The sign-in only decides *who* you are. Whether you may use the app follows from one Authentik group
(OIDC_ADMIN_GROUP) and from being linked to a local account: a group member is not enough by itself, their
Authentik subject ("sub" claim) must already be on a user row (users.oidc_sub), set with
`python -m grocery.cli link-oidc <username> <sub>` the same way `create-user --tailscale-login` links
TS_IDENTITY_MODE=sso. This app already supports several named accounts, unlike the single fixed admin some
of the other apps on this stack use, so a group membership alone does not say which local account it is.

Only this server talks to Authentik's token endpoint, through OIDC_INTERNAL_URL when Authentik is not
reachable by its public name from inside this container (the shared docker network `identity-apps`); the
browser is always sent to the public address. The id_token comes straight from the token endpoint over a
direct server-to-server call, which OpenID Connect Core 3.1.3.7 accepts instead of checking its signature;
issuer, audience, expiry and nonce are checked here.

The sign-in in progress (state, nonce, PKCE verifier, where to go afterwards) lives in a short-lived signed
cookie, so nothing is stored server-side before the person is authenticated. Same pattern as Clothing
Advisor's clothing_advisor/oidc.py and TrendWatcher's app/oidc.py.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlparse

from grocery.config import Settings

log = logging.getLogger(__name__)

PENDING_COOKIE = "oidc_pending"
PENDING_TTL = 600  # seconds to finish a sign-in once started
META_TTL = 3600
MAX_BODY = 1_000_000

# (method, url, headers, form body) -> (status, body); replaced in tests
Fetch = Callable[[str, str, dict[str, str], dict[str, str] | None], tuple[int, bytes]]


class OidcError(Exception):
    """The sign-in failed (bad state, bad token, Authentik unreachable...). The message is for the log, not the user."""


class OidcDenied(OidcError):
    """Signed in fine, but this person may not use the app (wrong group, or not linked to a local account)."""


def _urllib_fetch(method: str, url: str, headers: dict[str, str], data: dict[str, str] | None) -> tuple[int, bytes]:
    body = urlencode(data).encode() if data is not None else None
    if body is not None:
        headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_BODY)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def enabled(settings: Settings) -> bool:
    return bool(settings.oidc_issuer)


class Oidc:
    def __init__(self, settings: Settings, fetch: Fetch | None = None, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        # Looked up by name here rather than as a `fetch: Fetch = _urllib_fetch` default (which would freeze the
        # reference at import time): tests patch the module-level `_urllib_fetch` to cover the routes, which
        # construct Oidc(settings) with no fetch of their own.
        self._fetch = fetch if fetch is not None else _urllib_fetch
        self._now = clock
        self._meta: dict[str, Any] | None = None
        self._meta_at = 0.0
        # Domain-separated from the client secret, so a pending-sign-in cookie can never be replayed as it.
        secret = settings.oidc_client_secret.get_secret_value() if settings.oidc_client_secret else ""
        self._key = hashlib.sha256(b"grocery-agent oidc pending v1|" + secret.encode()).digest()

    # ------------------------------------------------------------------ the pending-sign-in cookie
    def _sign(self, payload: dict[str, Any]) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        return f"{body}.{_b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())}"

    def _unsign(self, value: str) -> dict[str, Any] | None:
        try:
            body, sig = value.split(".", 1)
            if not hmac.compare_digest(_b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest()), sig):
                return None
            payload = json.loads(_unb64(body))
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or float(payload.get("exp", 0)) <= self._now():
            return None
        return payload

    # ------------------------------------------------------------------ talking to Authentik
    def _request(self, method: str, url: str, data: dict[str, str] | None = None) -> tuple[int, bytes]:
        """One call to Authentik. With OIDC_INTERNAL_URL the request goes to that address but still names the
        public host (Host + X-Forwarded-Proto), so Authentik builds the same issuer and URLs as for the browser."""
        headers = {"Accept": "application/json", "User-Agent": "grocery-agent"}
        if self.settings.oidc_internal_url:
            public, internal = urlparse(url), urlparse(self.settings.oidc_internal_url)
            url = public._replace(scheme=internal.scheme, netloc=internal.netloc).geturl()
            headers.update({"Host": public.netloc, "X-Forwarded-Proto": public.scheme})
        try:
            return self._fetch(method, url, headers, data)
        except (OSError, urllib.error.URLError) as exc:
            raise OidcError(f"cannot reach the sign-in service: {exc}") from exc

    def _metadata(self) -> dict[str, Any]:
        if self._meta and self._now() - self._meta_at < META_TTL:
            return self._meta
        issuer = self.settings.oidc_issuer
        status, body = self._request("GET", issuer.rstrip("/") + "/.well-known/openid-configuration")
        if status != 200:
            raise OidcError(f"discovery answered {status}")
        try:
            meta = json.loads(body)
        except ValueError as exc:
            raise OidcError("discovery did not return JSON") from exc
        if str(meta.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
            raise OidcError(
                f"issuer mismatch: OIDC_ISSUER is {issuer!r} but Authentik says {meta.get('issuer')!r} "
                "(through OIDC_INTERNAL_URL, check that the public host is passed on)"
            )
        for key in ("authorization_endpoint", "token_endpoint"):
            if not meta.get(key):
                raise OidcError(f"discovery has no {key}")
        self._meta, self._meta_at = meta, self._now()
        return meta

    # ------------------------------------------------------------------ the sign-in flow
    def redirect_uri_for(self, host: str) -> str:
        """The registered callback on the address the browser is using: it must come back to the same host."""
        uris = self.settings.oidc_redirect_uri_list
        for uri in uris:
            if urlparse(uri).netloc.lower() == host.strip().lower():
                return uri
        return uris[0]

    def start(self, host: str, next_path: str) -> tuple[str, str]:
        """(where to send the browser, the signed pending-sign-in cookie to set)."""
        meta = self._metadata()
        redirect_uri = self.redirect_uri_for(host)
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        cookie = self._sign({
            "state": state, "nonce": nonce, "verifier": verifier, "next": next_path,
            "redirect_uri": redirect_uri, "exp": self._now() + PENDING_TTL,
        })
        query = urlencode({
            "response_type": "code", "client_id": self.settings.oidc_client_id, "redirect_uri": redirect_uri,
            "scope": "openid profile email", "state": state, "nonce": nonce,
            "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
        })
        endpoint = meta["authorization_endpoint"]
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}", cookie

    def finish(self, code: str, state: str, cookie: str | None) -> tuple[str, str]:
        """Exchange the code and check who this is: (Authentik subject, where to go next)."""
        pending = self._unsign(cookie) if cookie else None
        if not pending:
            raise OidcError("no sign-in in progress (cookie missing, expired or tampered with)")
        if not code or not state or not hmac.compare_digest(str(pending["state"]), state):
            raise OidcError("state mismatch")

        meta = self._metadata()
        status, body = self._request("POST", meta["token_endpoint"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
            "client_id": self.settings.oidc_client_id,
            "client_secret": self.settings.oidc_client_secret.get_secret_value() if self.settings.oidc_client_secret else "",
            "code_verifier": pending["verifier"],
        })
        if status != 200:
            raise OidcError(f"token endpoint answered {status}: {body[:200]!r}")
        try:
            claims = _claims(json.loads(body)["id_token"])
        except (ValueError, KeyError, TypeError) as exc:
            raise OidcError(f"unusable token response: {exc}") from exc

        now = self._now()
        if str(claims.get("iss", "")).rstrip("/") != self.settings.oidc_issuer.rstrip("/"):
            raise OidcError(f"wrong issuer in id_token: {claims.get('iss')!r}")
        aud = claims.get("aud")
        if self.settings.oidc_client_id not in (aud if isinstance(aud, list) else [aud]):
            raise OidcError(f"id_token is not for this client: aud={aud!r}")
        if float(claims.get("exp", 0)) <= now - 60:
            raise OidcError("id_token has expired")
        if not hmac.compare_digest(str(claims.get("nonce", "")), str(pending["nonce"])):
            raise OidcError("nonce mismatch")
        sub = str(claims.get("sub", ""))
        if not sub:
            raise OidcError("id_token has no sub")

        groups = claims.get("groups")
        if not isinstance(groups, list) or self.settings.oidc_admin_group not in {str(g) for g in groups}:
            raise OidcDenied(f"{sub} is not in {self.settings.oidc_admin_group!r}")
        return sub, str(pending["next"])


def _claims(id_token: str) -> dict[str, Any]:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("id_token is not a JWT")
    claims = json.loads(_unb64(parts[1]))
    if not isinstance(claims, dict):
        raise ValueError("id_token payload is not an object")
    return claims
