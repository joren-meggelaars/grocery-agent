import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=WEB_DIR / "templates")


@lru_cache
def _digest(rel: str) -> str:
    return hashlib.sha1((STATIC_DIR / rel).read_bytes()).hexdigest()[:10]


def static_url(rel: str) -> str:
    """Content-hashed URL, so a changed asset is never served stale from the phone cache."""
    return f"/static/{rel}?v={_digest(rel)}"


templates.env.globals["static_url"] = static_url
templates.env.globals["app_timezone"] = "Europe/Amsterdam"  # replaced from Settings.timezone in create_app


def localtime(value: datetime | None, fmt: str = "%d %b %H:%M") -> str:
    """A stored UTC moment shown in the user's own timezone (summer and winter time included)."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(templates.env.globals["app_timezone"])).strftime(fmt)


templates.env.filters["localtime"] = localtime
templates.env.globals["basis_label"] = {"pack": "per pack", "kg": "per kg", "l": "per l"}
