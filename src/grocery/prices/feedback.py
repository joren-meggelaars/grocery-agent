"""Instant feedback after saving a shelf price: what I usually pay, and where it is cheaper."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.db.models import PriceObservation
from grocery.prices.observations import comparable

USUAL_WINDOW_DAYS = 180
CHEAPER_WINDOW_DAYS = 60
USUAL_SAMPLE = 5
BAND = 0.05  # within +-5% of the usual price counts as "usual"


@dataclass
class Seen:
    store: str
    value: int
    observed_on: date
    source: str
    promo: bool


@dataclass
class Feedback:
    verdict: str  # first | cheaper | usual | pricier
    basis: str  # pack | kg | l
    this_value: int
    usual: int | None = None
    usual_count: int = 0
    pct_vs_usual: float | None = None
    last_paid: Seen | None = None
    cheaper_elsewhere: list[Seen] = field(default_factory=list)


def _seen(o: PriceObservation, value: int) -> Seen:
    return Seen(o.store.name, value, o.observed_on, o.source, o.is_promo)


def feedback_for(
    db: Session,
    product_id: int,
    store_id: int,
    basis: str,
    value: int,
    today: date,
    exclude: tuple[str, int] | None = None,
) -> Feedback:
    since = today - timedelta(days=USUAL_WINDOW_DAYS)
    rows = db.scalars(
        select(PriceObservation)
        .where(PriceObservation.product_id == product_id, PriceObservation.observed_on >= since)
        .order_by(PriceObservation.observed_on.desc(), PriceObservation.id.desc())
    ).all()
    same = []
    for o in rows:
        if exclude and (o.source, o.source_ref_id) == exclude:
            continue
        o_basis, o_value = comparable(o.price_cents, o.unit_price_cents, o.unit_basis)
        if o_basis == basis:
            same.append((o, o_value))

    result = Feedback(verdict="first", basis=basis, this_value=value)

    # "Usual" is what I actually paid; shelf sightings only stand in when there is no receipt yet.
    paid = [(o, v) for o, v in same if o.source == "receipt"] or [(o, v) for o, v in same if not o.is_promo] or same
    if paid:
        recent = [v for _, v in paid[:USUAL_SAMPLE]]
        result.usual = round(median(recent))
        result.usual_count = len(recent)
        result.pct_vs_usual = (value - result.usual) / result.usual if result.usual else None
        result.last_paid = _seen(*paid[0])
        ratio = value / result.usual if result.usual else 1.0
        result.verdict = "cheaper" if ratio <= 1 - BAND else "pricier" if ratio >= 1 + BAND else "usual"

    recent_cutoff = today - timedelta(days=CHEAPER_WINDOW_DAYS)
    best_per_store: dict[int, tuple[PriceObservation, int]] = {}
    for o, v in same:
        if o.store_id == store_id or o.observed_on < recent_cutoff or v >= value:
            continue
        if o.valid_until is not None and o.valid_until < today:
            continue  # the promotion has ended
        if o.store_id not in best_per_store or v < best_per_store[o.store_id][1]:
            best_per_store[o.store_id] = (o, v)
    result.cheaper_elsewhere = [_seen(o, v) for o, v in sorted(best_per_store.values(), key=lambda t: t[1])[:3]]
    return result
