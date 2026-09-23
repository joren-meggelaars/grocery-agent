from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grocery.capture.service import local_date
from grocery.db.base import utcnow
from grocery.db.models import Deal, DealRun, Job, Store
from grocery.deals import service
from grocery.security.deps import Principal, current_principal, get_db
from grocery.settings_store import effective
from grocery.web.templating import templates

router = APIRouter()

REFRESH_COOLDOWN = timedelta(minutes=30)
NOTICES = {
    "queued": "Refresh started. Come back in a few minutes.",
    "busy": "A refresh is already running.",
    "recent": "Refreshed less than 30 minutes ago. The source asks us to go easy.",
    "off": "The deals radar is switched off in Settings.",
}


@router.get("/deals", response_class=HTMLResponse)
def deals_page(
    request: Request, refresh: str | None = None,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    settings = request.app.state.settings
    eff = effective(db, settings)
    today = local_date(utcnow(), eff.timezone)
    templates.env.globals["app_timezone"] = eff.timezone
    current = (Deal.valid_until.is_(None)) | (Deal.valid_until >= today)
    ordering = (Deal.vs_usual_pct.asc().nulls_last(), Deal.savings_pct.desc().nulls_last(), Deal.id)
    mine = db.scalars(select(Deal).where(Deal.relevance == "mine", current).order_by(*ordering)).all()
    notable = db.scalars(select(Deal).where(Deal.relevance == "notable", current).order_by(*ordering)).all()
    last = db.scalar(select(DealRun).order_by(DealRun.id.desc()).limit(1))
    return templates.TemplateResponse(
        request, "deals.html",
        {
            "user": principal.user, "csrf_token": principal.csrf_token, "eff": eff, "mine": mine, "notable": notable,
            "total": db.scalar(select(func.count(Deal.id)).where(current)) or 0, "last": last,
            "stores": {s.chain: s.name for s in db.scalars(select(Store))},
            "ha_ready": service.ha_configured(settings), "notice": NOTICES.get(refresh or ""),
            "source_name": service.SOURCE_NAME,
        },
    )


@router.post("/deals/refresh")
def deals_refresh(request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    eff = effective(db, request.app.state.settings)
    now = utcnow()
    if eff.deals_enabled != "on":
        return RedirectResponse("/deals?refresh=off", status_code=303)
    if db.scalar(select(Job.id).where(Job.kind == service.JOB_KIND, Job.status.in_(("queued", "running"))).limit(1)):
        return RedirectResponse("/deals?refresh=busy", status_code=303)
    last = db.scalar(select(DealRun).order_by(DealRun.id.desc()).limit(1))
    if last is not None and now - last.started_at < REFRESH_COOLDOWN:
        return RedirectResponse("/deals?refresh=recent", status_code=303)
    service.ensure_refresh_queued(db, eff, now, force=True)
    return RedirectResponse("/deals?refresh=queued", status_code=303)
