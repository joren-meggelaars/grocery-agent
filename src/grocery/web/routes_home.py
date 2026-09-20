from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from grocery.capture.service import review_count
from grocery.llm.budget import budget_state
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
def home(
    request: Request,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": principal.user,
            "csrf_token": principal.csrf_token,
            "budget": budget_state(db, request.app.state.settings),
            "to_review": review_count(db),
        },
    )
