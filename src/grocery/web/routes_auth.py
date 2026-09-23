import logging
import math

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.db.models import AuthSession, User
from grocery.security import ratelimit
from grocery.security.deps import Principal, current_principal, get_db, public
from grocery.security.passwords import hash_password, needs_rehash, verify_password
from grocery.security.sessions import create_session, revoke_all, revoke_session
from grocery.settings_store import effective
from grocery.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()


def _safe_next(value: str | None) -> str:
    """Only local paths: blocks open redirects like //evil.example or /\\evil.example."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _login_page(request: Request, next_url: str, error: str | None = None, status: int = 200):
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "next": next_url}, status_code=status
    )


def _start_session(request: Request, db: Session, user: User, next_url: str) -> RedirectResponse:
    settings = request.app.state.settings
    token, _ = create_session(db, user, settings, request.headers.get("user-agent"))
    response = RedirectResponse(_safe_next(next_url), status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_absolute_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


@router.get("/login", response_class=HTMLResponse)
@public
def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    settings = request.app.state.settings
    if settings.ts_identity_mode == "sso":
        login = getattr(request.state, "ts_login", None)
        user = (
            db.scalar(select(User).where(User.tailscale_login == login, User.is_active.is_(True)))
            if login
            else None
        )
        if user is not None:
            ratelimit.record(db, "login_ok", user.username, _client_ip(request), "tailscale sso")
            return _start_session(request, db, user, next)
    return _login_page(request, _safe_next(next))


@router.post("/login", response_class=HTMLResponse)
@public
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    settings = request.app.state.settings
    ip = _client_ip(request)
    name = username.strip().lower()[:64]
    next_url = _safe_next(next)

    if ratelimit.globally_limited(db):
        ratelimit.record(db, "lockout", name, ip, "global")
        return _login_page(request, next_url, "Too many failed attempts. Try again in a few minutes.", 429)

    wait = ratelimit.lockout_remaining(db, name)
    if wait:
        ratelimit.record(db, "lockout", name, ip)
        minutes = max(1, math.ceil(wait / 60))
        return _login_page(request, next_url, f"Too many attempts. Try again in {minutes} minute(s).", 429)

    user = db.scalar(select(User).where(User.username == name))
    active = user is not None and user.is_active
    ok = verify_password(user.password_hash if active else None, password)
    if ok and settings.ts_identity_mode == "require" and user.tailscale_login:
        # A user bound to a Tailscale login must also arrive with that identity.
        ok = getattr(request.state, "ts_login", None) == user.tailscale_login.lower()

    if not ok:
        ratelimit.record(db, "login_fail", name, ip)
        log.info("login failed for %r from %s", name, ip)
        return _login_page(request, next_url, "Invalid username or password.", 401)

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
    ratelimit.record(db, "login_ok", name, ip)
    return _start_session(request, db, user, next_url)


@router.post("/logout")
def logout(
    request: Request,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    settings = request.app.state.settings
    revoke_session(db, principal.session.id, principal.user.id)
    ratelimit.record(db, "logout", principal.user.username, _client_ip(request))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/account", response_class=HTMLResponse)
def account(
    request: Request,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    sessions = db.scalars(
        select(AuthSession)
        .where(AuthSession.user_id == principal.user.id)
        .order_by(AuthSession.last_seen.desc())
    ).all()
    templates.env.globals["app_timezone"] = effective(db, request.app.state.settings).timezone
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "user": principal.user,
            "csrf_token": principal.csrf_token,
            "sessions": sessions,
            "current_id": principal.session.id,
            "ts_login": getattr(request.state, "ts_login", None),
            "ts_mode": request.app.state.settings.ts_identity_mode,
        },
    )


@router.post("/account/sessions/{session_id}/revoke")
def revoke_one(
    session_id: int,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    if session_id == principal.session.id:
        return RedirectResponse("/account", status_code=303)  # use Log out for the current one
    revoke_session(db, session_id, principal.user.id)
    return RedirectResponse("/account", status_code=303)


@router.post("/account/sessions/revoke-others")
def revoke_others(
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    revoke_all(db, principal.user.id, except_id=principal.session.id)
    return RedirectResponse("/account", status_code=303)
