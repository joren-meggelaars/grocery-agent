from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grocery.config import Settings
from grocery.db.base import utcnow
from grocery.db.models import Extraction


def month_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend_eur(db: Session, now: datetime | None = None) -> float:
    now = now or utcnow()
    total = db.scalar(
        select(func.coalesce(func.sum(Extraction.cost_est_eur), 0.0)).where(
            Extraction.created_at >= month_start(now)
        )
    )
    return float(total or 0.0)


def budget_state(db: Session, settings: Settings, now: datetime | None = None, cap: float | None = None) -> dict:
    """cap overrides settings.llm_monthly_budget_eur, for the value the Settings page may have set."""
    spent = month_spend_eur(db, now)
    cap = settings.llm_monthly_budget_eur if cap is None else cap
    ratio = spent / cap if cap > 0 else 1.0
    return {
        "spent": spent,
        "cap": cap,
        "ratio": ratio,
        "warn": ratio >= 0.8,  # alert at 80%
        "exhausted": ratio >= 1.0,  # hard stop at 100%
    }
