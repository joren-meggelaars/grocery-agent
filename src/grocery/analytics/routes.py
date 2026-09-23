import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from grocery.analytics import charts
from grocery.analytics.aggregate import (
    PERIODS,
    add_months,
    against_reference,
    current_cycle_anchor,
    cycle_bounds,
    month_series,
    period_start,
    project_month_end,
    summarize_month,
    top_products,
    trend,
)
from grocery.analytics.queries import load_categories, load_receipts
from grocery.capture.service import local_date
from grocery.db.base import utcnow
from grocery.prices.feedback import price_overview
from grocery.security.deps import Principal, current_principal, get_db
from grocery.settings_store import effective
from grocery.web.templating import templates

router = APIRouter()
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
TREND_MONTHS = 6


def parse_month(value: str | None, current: date) -> date:
    """A YYYY-MM query value, clamped to the current cycle's anchor month; anything else means the current cycle."""
    match = _MONTH.match(value or "")
    if not match:
        return current
    year, month = int(match.group(1)), int(match.group(2))
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return current
    return min(date(year, month, 1), current)


@router.get("/overview", response_class=HTMLResponse)
def overview(
    request: Request,
    month: str | None = None,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    settings = request.app.state.settings
    eff = effective(db, settings)
    today = local_date(utcnow(), eff.timezone)
    current = current_cycle_anchor(today, eff.cycle_start_day)
    selected = parse_month(month, current)
    is_current = selected == current

    window_start, _ = cycle_bounds(add_months(selected, -(TREND_MONTHS - 1)), eff.cycle_start_day)
    _, cycle_end = cycle_bounds(selected, eff.cycle_start_day)
    receipts = load_receipts(db, since=window_start, until=cycle_end)
    categories = load_categories(db)
    summary = summarize_month(receipts, categories, selected, eff.cycle_start_day)
    series = month_series(receipts, categories, selected, TREND_MONTHS, eff.cycle_start_day)
    reference_cents = round(eff.monthly_reference_eur * 100)

    return templates.TemplateResponse(
        request,
        "analytics/overview.html",
        {
            "user": principal.user,
            "month": selected,
            "cycle_start": summary.month,
            "cycle_last_day": cycle_end - timedelta(days=1),
            "prev_month": add_months(selected, -1).strftime("%Y-%m"),
            "next_month": add_months(selected, 1).strftime("%Y-%m") if selected < current else None,
            "summary": summary,
            "trend": trend(series),
            "reference": against_reference(summary.food_cents, reference_cents),
            "projection": project_month_end(summary.food_cents, selected, today, eff.cycle_start_day),
            "days_left": (cycle_end - today).days - 1 if is_current else None,
            "series": series,
            "trend_chart": charts.column_chart(series, reference_cents),
            "category_chart": charts.bar_chart(summary.by_category, "Spend per category this month"),
            "store_chart": charts.bar_chart(summary.by_store, "Spend per store this month"),
            "eur": charts.eur,
        },
    )


@router.get("/regulars", response_class=HTMLResponse)
def regulars(
    request: Request,
    period: str = "3m",
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    period = period if period in PERIODS else "3m"
    eff = effective(db, request.app.state.settings)
    today = local_date(utcnow(), eff.timezone)
    since = period_start(period, today, eff.cycle_start_day)
    stats, unmatched = top_products(load_receipts(db, since=since), since)
    overview = price_overview(db, [s.product_id for s in stats], today)
    return templates.TemplateResponse(
        request,
        "analytics/regulars.html",
        {
            "user": principal.user,
            "period": period,
            "periods": PERIODS,
            "stats": stats,
            "overview": overview,
            "unmatched": unmatched,
            "eur": charts.eur,
        },
    )
