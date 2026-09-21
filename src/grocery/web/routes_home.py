from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from grocery.analytics.aggregate import add_months, against_reference, first_of_month, summarize_month
from grocery.analytics.charts import eur
from grocery.analytics.queries import load_categories, load_receipts
from grocery.capture.service import local_date, review_count
from grocery.db.base import utcnow
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
    settings = request.app.state.settings
    month = first_of_month(local_date(utcnow(), settings.timezone))
    summary = summarize_month(
        load_receipts(db, since=month, until=add_months(month, 1)), load_categories(db), month
    )
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": principal.user,
            "csrf_token": principal.csrf_token,
            "budget": budget_state(db, settings),
            "to_review": review_count(db),
            "month": month,
            "food_cents": summary.food_cents,
            "reference": against_reference(summary.food_cents, round(settings.monthly_reference_eur * 100)),
            "eur": eur,
        },
    )
