"""Deny-by-default authentication.

`require_auth` and `csrf_protect` are attached to the FastAPI app as *global*
dependencies, so every route is protected unless its endpoint is explicitly
marked with @public. tests/test_deny_by_default.py enumerates all routes and
fails if the public set drifts from the expected allowlist.
"""

import hmac
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from grocery.db.models import AuthSession, User
from grocery.security.sessions import lookup_session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def public(func: Callable) -> Callable:
    """Mark an endpoint as reachable without a session."""
    func.__public__ = True  # type: ignore[attr-defined]
    return func


def is_public(endpoint: Callable | None) -> bool:
    return bool(getattr(endpoint, "__public__", False))


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as db:
        yield db


@dataclass
class Principal:
    user: User
    session: AuthSession

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_secret


def _wants_html(request: Request) -> bool:
    return (
        request.method == "GET"
        and "text/html" in request.headers.get("accept", "")
        and "hx-request" not in request.headers
    )


def require_auth(request: Request, db: Session = Depends(get_db)) -> Principal | None:
    route = request.scope.get("route")
    if is_public(getattr(route, "endpoint", None)):
        return None

    settings = request.app.state.settings
    session = lookup_session(db, request.cookies.get(settings.session_cookie_name), settings)
    if session is None:
        if _wants_html(request):
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            raise HTTPException(303, headers={"Location": f"/login?next={quote(target, safe='')}"})
        raise HTTPException(401, "Authentication required")
    principal = Principal(user=session.user, session=session)
    request.state.principal = principal
    return principal


def current_principal(request: Request) -> Principal:
    """For endpoints that need the logged-in user (set by require_auth)."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(401, "Authentication required")
    return principal


async def csrf_protect(request: Request, principal: Principal | None = Depends(require_auth)) -> None:
    """Synchroniser token on unsafe methods for authenticated routes.

    The token comes from the X-CSRF-Token header (JS) or a csrf_token form field.
    Upload endpoints must use the header so the body is not buffered here.
    """
    if principal is None or request.method in SAFE_METHODS:
        return
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get("csrf_token")
            supplied = value if isinstance(value, str) else None
    if not supplied or not hmac.compare_digest(supplied, principal.csrf_token):
        raise HTTPException(403, "CSRF token missing or invalid")
