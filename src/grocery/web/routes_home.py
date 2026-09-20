from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from grocery.security.deps import Principal, current_principal, get_db, public
from grocery.web.templating import templates

router = APIRouter()


@router.get("/healthz")
@public
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "db_unavailable"}, status_code=503)
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
def home(request: Request, principal: Principal = Depends(current_principal)):
    return templates.TemplateResponse(
        request,
        "home.html",
        {"user": principal.user, "csrf_token": principal.csrf_token},
    )
