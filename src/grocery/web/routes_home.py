from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from grocery.analytics.aggregate import against_reference, current_cycle_anchor, cycle_bounds, summarize_month
from grocery.analytics.charts import eur
from grocery.analytics.queries import load_categories, load_receipts
from grocery.capture.service import local_date, review_count
from grocery.db.base import utcnow
from grocery.llm.budget import budget_state
from grocery.security.deps import Principal, current_principal, get_db, public
from grocery.settings_store import effective
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
    eff = effective(db, settings)
    today = local_date(utcnow(), eff.timezone)
    anchor = current_cycle_anchor(today, eff.cycle_start_day)
    start, end = cycle_bounds(anchor, eff.cycle_start_day)
    summary = summarize_month(
        load_receipts(db, since=start, until=end), load_categories(db), anchor, eff.cycle_start_day
    )
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": principal.user,
            "csrf_token": principal.csrf_token,
            "budget": budget_state(db, settings, cap=eff.llm_monthly_budget_eur),
            "to_review": review_count(db),
            "month": start,
            "food_cents": summary.food_cents,
            "reference": against_reference(summary.food_cents, round(eff.monthly_reference_eur * 100)),
            "eur": eur,
        },
    )
