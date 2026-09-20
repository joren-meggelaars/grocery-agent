"""Tailscale identity headers.

`tailscale serve` adds Tailscale-User-Login (and -Name, -Profile-Pic) to proxied
requests and strips client-supplied copies. From inside a container the direct
peer is the Docker bridge gateway, not 127.0.0.1, so trust is granted only when
the peer address is in TS_TRUSTED_PROXY_IPS. Anything else on that network (or on
the host) could still forge the header, which is why the default mode `require`
never lets the header replace the password.
"""

import logging
from ipaddress import ip_address

from starlette.requests import Request

from grocery.config import Settings

log = logging.getLogger(__name__)

LOGIN_HEADER = "tailscale-user-login"


def peer_is_trusted(request: Request, settings: Settings) -> bool:
    if request.client is None:
        return False
    try:
        peer = ip_address(request.client.host)
    except ValueError:
        return False
    return any(peer in net for net in settings.trusted_networks)


def identity(request: Request, settings: Settings) -> str | None:
    """Tailscale login of the caller, or None if absent or not from a trusted peer."""
    value = request.headers.get(LOGIN_HEADER)
    if not value:
        return None
    if not peer_is_trusted(request, settings):
        log.warning(
            "ignoring %s header from untrusted peer %s (check TS_TRUSTED_PROXY_IPS)",
            LOGIN_HEADER,
            request.client.host if request.client else "?",
        )
        return None
    return value.strip().lower() or None


def identity_allowed(login: str | None, settings: Settings) -> bool:
    return login is not None and login in settings.ts_allowed_logins
