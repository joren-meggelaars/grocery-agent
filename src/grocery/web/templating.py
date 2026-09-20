import hashlib
from functools import lru_cache
from pathlib import Path

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
