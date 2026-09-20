"""Cross-cutting request checks, applied to every route (public ones included).

Order: Host allowlist -> Tailscale identity gate -> Origin / Sec-Fetch-Site check on
unsafe methods -> route -> security headers on the way out. /healthz is exempt from
the first two so the container healthcheck works without headers.
"""

import logging
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from grocery.security import tailscale

log = logging.getLogger(__name__)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
HEALTH_PATH = "/healthz"

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
    "manifest-src 'self'; worker-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",
    "X-Frame-Options": "DENY",
}


def _finish(response: Response, request: Request) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


def _deny(request: Request, status: int, message: str) -> Response:
    return _finish(PlainTextResponse(message, status_code=status), request)


def _request_host(request: Request) -> str:
    return request.headers.get("host", "").split(":")[0].lower()


def _origin_ok(request: Request, allowed_hosts: list[str]) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        # Browsers always send Origin on POST; only accept its absence if the
        # browser vouched for the request via Sec-Fetch-Site.
        return fetch_site is not None
    origin_host = urlsplit(origin).hostname
    if not origin_host:
        return False
    allowed = set(allowed_hosts) or {_request_host(request)}
    return origin_host.lower() in allowed


def install_security_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def security(request: Request, call_next):
        settings = request.app.state.settings
        path = request.url.path

        if path != HEALTH_PATH:
            if settings.allowed_hosts and _request_host(request) not in settings.allowed_hosts:
                log.warning("rejected Host header %r (add it to ALLOWED_HOSTS?)", _request_host(request))
                return _deny(request, 400, "Invalid host header")

            if settings.ts_identity_mode != "off":
                login = tailscale.identity(request, settings)
                if not tailscale.identity_allowed(login, settings):
                    log.warning("tailscale identity rejected (login=%r)", login)
                    return _deny(request, 403, "Forbidden")
                request.state.ts_login = login

        if request.method in UNSAFE_METHODS and not _origin_ok(request, settings.allowed_hosts):
            log.warning("rejected cross-origin %s %s", request.method, path)
            return _deny(request, 403, "Cross-origin request rejected")

        return _finish(await call_next(request), request)
