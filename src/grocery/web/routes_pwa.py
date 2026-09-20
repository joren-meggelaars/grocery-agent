"""Service worker and manifest. They contain nothing private, and the browser fetches them
without the page's credentials in some cases, so they are explicitly public."""

from fastapi import APIRouter
from fastapi.responses import FileResponse

from grocery.security.deps import public
from grocery.web.templating import STATIC_DIR

router = APIRouter()


@router.get("/sw.js")
@public
def service_worker():
    # Served from the site root so its scope can cover /capture (a worker under /static/ could not).
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers={"Service-Worker-Allowed": "/"})


@router.get("/manifest.webmanifest")
@public
def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")
