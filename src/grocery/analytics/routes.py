import re
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from grocery.analytics import charts
from grocery.analytics.aggregate import (
    PERIODS,
    add_months,
    against_reference,
    days_in_month,
    first_of_month,
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
from grocery.security.deps import Principal, current_principal, get_db
from grocery.web.templating import templates

router = APIRouter()
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
TREND_MONTHS = 6


def parse_month(value: str | None, today: date) -> date:
    """A YYYY-MM query value, clamped to the current month; anything else means the current month."""
    current = first_of_month(today)
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
    today = local_date(utcnow(), settings.timezone)
    selected = parse_month(month, today)
    current = first_of_month(today)

    receipts = load_receipts(db, since=add_months(selected, -(TREND_MONTHS - 1)), until=add_months(selected, 1))
    categories = load_categories(db)
    summary = summarize_month(receipts, categories, selected)
    series = month_series(receipts, categories, selected, TREND_MONTHS)
    reference_cents = round(settings.monthly_reference_eur * 100)
    is_current = selected == current

    return templates.TemplateResponse(
        request,
        "analytics/overview.html",
        {
            "user": principal.user,
            "month": selected,
            "prev_month": add_months(selected, -1).strftime("%Y-%m"),
            "next_month": add_months(selected, 1).strftime("%Y-%m") if selected < current else None,
            "summary": summary,
            "trend": trend(series),
            "reference": against_reference(summary.food_cents, reference_cents),
            "projection": project_month_end(summary.food_cents, selected, today),
            "days_left": days_in_month(selected) - today.day if is_current else None,
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
    today = local_date(utcnow(), request.app.state.settings.timezone)
    since = period_start(period, today)
    stats, unmatched = top_products(load_receipts(db, since=since), since)
    return templates.TemplateResponse(
        request,
        "analytics/regulars.html",
        {
            "user": principal.user,
            "period": period,
            "periods": PERIODS,
            "stats": stats,
            "unmatched": unmatched,
            "eur": charts.eur,
        },
    )
