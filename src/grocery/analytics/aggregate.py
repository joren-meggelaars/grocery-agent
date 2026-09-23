"""Monthly overview and "regulars": pure functions over plain facts, no database.

Money rules
- A month is the calendar month of the purchase date on the receipt.
- The month total is the sum of receipt totals, so it matches what left the wallet.
- Category amounts come from the lines. A discount line is booked to the category of the item line
  before it (receipts print a discount right under the product it applies to); deposits, bags and
  rounding go to the fallback category. Whatever the lines do not explain (a receipt saved with
  "the lines do not add up") is shown as "Not itemised" so the numbers always add up to the total.
- "Food spend" is what is compared with the monthly reference: every category flagged
  counts_as_food, plus the not-itemised remainder.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

from grocery.refdata import FALLBACK_CATEGORY

NOT_ITEMISED = "Not itemised"
PROJECTION_MIN_DAYS = 7  # earlier in the month a pace estimate says nothing


@dataclass(frozen=True)
class LineFact:
    kind: str
    amount_cents: int
    category_id: int | None
    product_id: int | None = None
    product_name: str | None = None
    quantity_milli: int = 1000
    unit: str = "pcs"


@dataclass(frozen=True)
class ReceiptFact:
    id: int
    on: date
    store_name: str
    total_cents: int
    lines: tuple[LineFact, ...] = ()


@dataclass(frozen=True)
class CategoryMeta:
    id: int
    name: str
    counts_as_food: bool


@dataclass
class Row:
    label: str
    cents: int
    share: float  # of the month total, 0..1
    food: bool = True


@dataclass
class MonthSummary:
    month: date  # first day
    receipts: int = 0
    total_cents: int = 0
    food_cents: int = 0
    nonfood_cents: int = 0
    not_itemised_cents: int = 0
    by_store: list[Row] = field(default_factory=list)
    by_category: list[Row] = field(default_factory=list)


@dataclass(frozen=True)
class MonthPoint:
    month: date
    total_cents: int
    food_cents: int
    receipts: int


# --- calendar ---------------------------------------------------------------

def first_of_month(day: date) -> date:
    return day.replace(day=1)


def add_months(month: date, delta: int) -> date:
    index = month.year * 12 + (month.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def days_in_month(month: date) -> int:
    return (add_months(month, 1) - month).days


# --- the spending cycle: a calendar month by default, or payday to payday ---------------------

def cycle_start(anchor: date, start_day: int) -> date:
    """The date the cycle labelled by anchor's calendar month begins on.

    start_day is clamped to the days that month actually has, so day 30 or 31 still works in
    February. start_day=1 makes a cycle the same as a calendar month.
    """
    return anchor.replace(day=min(start_day, days_in_month(first_of_month(anchor))))


def cycle_bounds(anchor: date, start_day: int) -> tuple[date, date]:
    """[start, end) of the cycle labelled by anchor's calendar month."""
    start = cycle_start(anchor, start_day)
    return start, cycle_start(add_months(first_of_month(anchor), 1), start_day)


def current_cycle_anchor(today: date, start_day: int) -> date:
    """The calendar month that labels the cycle `today` falls in."""
    anchor = first_of_month(today)
    if today.day < cycle_start(anchor, start_day).day:
        anchor = add_months(anchor, -1)
    return anchor


# --- attribution and summary -------------------------------------------------

def attribute(lines: tuple[LineFact, ...] | list[LineFact]) -> list[tuple[int | None, int]]:
    """(category_id, cents) per line, in order. None means the fallback category."""
    out: list[tuple[int | None, int]] = []
    last_item_category: int | None = None
    for line in lines:
        if line.kind == "item":
            last_item_category = line.category_id
            out.append((line.category_id, line.amount_cents))
        elif line.kind == "discount":
            out.append((last_item_category, line.amount_cents))
        else:  # deposit, bag, rounding
            out.append((None, line.amount_cents))
    return out


def _fallback_id(categories: dict[int, CategoryMeta]) -> int | None:
    return next((c.id for c in categories.values() if c.name == FALLBACK_CATEGORY), None)


def summarize_month(
    receipts: list[ReceiptFact], categories: dict[int, CategoryMeta], month: date, start_day: int = 1
) -> MonthSummary:
    start, end = cycle_bounds(month, start_day)
    fallback = _fallback_id(categories)
    per_category: dict[int | None, int] = defaultdict(int)
    per_store: dict[str, int] = defaultdict(int)
    summary = MonthSummary(month=start)

    for r in receipts:
        if not (start <= r.on < end):
            continue
        summary.receipts += 1
        summary.total_cents += r.total_cents
        per_store[r.store_name] += r.total_cents
        explained = 0
        for category_id, cents in attribute(r.lines):
            per_category[category_id if category_id in categories else fallback] += cents
            explained += cents
        summary.not_itemised_cents += r.total_cents - explained

    total = summary.total_cents
    share = (lambda cents: cents / total) if total > 0 else (lambda cents: 0.0)

    food = summary.not_itemised_cents
    nonfood = 0
    rows: list[Row] = []
    for category_id, cents in per_category.items():
        meta = categories.get(category_id)
        counts = meta.counts_as_food if meta else True
        food, nonfood = (food + cents, nonfood) if counts else (food, nonfood + cents)
        rows.append(Row(meta.name if meta else FALLBACK_CATEGORY, cents, share(cents), counts))
    if summary.not_itemised_cents:
        rows.append(Row(NOT_ITEMISED, summary.not_itemised_cents, share(summary.not_itemised_cents), True))

    summary.food_cents, summary.nonfood_cents = food, nonfood
    summary.by_category = sorted((r for r in rows if r.cents != 0), key=lambda r: (-r.cents, r.label))
    summary.by_store = sorted(
        (Row(name, cents, share(cents)) for name, cents in per_store.items() if cents != 0),
        key=lambda r: (-r.cents, r.label),
    )
    return summary


def month_series(
    receipts: list[ReceiptFact], categories: dict[int, CategoryMeta], last_month: date, months: int = 6,
    start_day: int = 1,
) -> list[MonthPoint]:
    """The last `months` cycles ending at last_month, oldest first, empty cycles included as zero."""
    points = []
    for offset in range(months - 1, -1, -1):
        month = add_months(first_of_month(last_month), -offset)
        s = summarize_month(receipts, categories, month, start_day)
        points.append(MonthPoint(s.month, s.total_cents, s.food_cents, s.receipts))
    return points


# --- trend, reference, pace --------------------------------------------------

@dataclass(frozen=True)
class Trend:
    previous_food_cents: int | None  # the month before, None if it had no receipts
    vs_previous_cents: int | None
    vs_previous_pct: float | None
    average_food_cents: int | None  # mean of the (up to) three months before, ignoring empty ones
    vs_average_cents: int | None


def trend(series: list[MonthPoint]) -> Trend:
    """Compare the last point with the months before it."""
    *history, current = series
    prev = history[-1] if history else None
    previous = prev.food_cents if prev and prev.receipts else None
    recent = [p.food_cents for p in history[-3:] if p.receipts]
    average = round(sum(recent) / len(recent)) if recent else None
    return Trend(
        previous_food_cents=previous,
        vs_previous_cents=current.food_cents - previous if previous is not None else None,
        vs_previous_pct=(current.food_cents - previous) / previous if previous else None,
        average_food_cents=average,
        vs_average_cents=current.food_cents - average if average is not None else None,
    )


@dataclass(frozen=True)
class Reference:
    reference_cents: int
    used_pct: float  # food spend / reference, may exceed 1
    remaining_cents: int  # negative when over
    state: str  # ok | warn | over  (warn from 80%)


def against_reference(food_cents: int, reference_cents: int) -> Reference:
    used = food_cents / reference_cents if reference_cents > 0 else 0.0
    state = "over" if used > 1 else "warn" if used >= 0.8 else "ok"
    return Reference(reference_cents, used, reference_cents - food_cents, state)


def project_month_end(food_cents: int, month: date, today: date, start_day: int = 1) -> int | None:
    """Food spend at the current pace, only for the running cycle and once a week of data exists."""
    start, end = cycle_bounds(month, start_day)
    if not (start <= today < end):
        return None
    elapsed = (today - start).days + 1
    if elapsed < PROJECTION_MIN_DAYS:
        return None
    return round(food_cents / elapsed * (end - start).days)


# --- regulars ----------------------------------------------------------------

@dataclass(frozen=True)
class ProductStat:
    product_id: int
    name: str
    times_bought: int  # receipts it appeared on
    total_cents: int
    last_on: date
    last_store: str
    typical_price_cents: int | None  # median per pack, or per kg for weighed goods
    basis: str  # pack | kg


PERIODS = {"month": "This month", "3m": "Last 3 months", "12m": "Last 12 months", "all": "All time"}


def period_start(period: str, today: date, start_day: int = 1) -> date | None:
    if period == "month":
        return cycle_start(current_cycle_anchor(today, start_day), start_day)
    if period == "3m":
        return today - timedelta(days=90)
    if period == "12m":
        return today - timedelta(days=365)
    return None


def top_products(
    receipts: list[ReceiptFact], since: date | None, limit: int = 25
) -> tuple[list[ProductStat], int]:
    """Most often bought products (by number of receipts) and how many item lines had no product yet."""
    seen_on: dict[int, set[int]] = defaultdict(set)
    totals: dict[int, int] = defaultdict(int)
    names: dict[int, str] = {}
    unit_prices: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    last: dict[int, tuple[date, int, str]] = {}
    unmatched = 0

    for r in receipts:
        if since is not None and r.on < since:
            continue
        for line in r.lines:
            if line.kind != "item":
                continue
            if line.product_id is None:
                unmatched += 1
                continue
            pid = line.product_id
            names.setdefault(pid, line.product_name or f"Product {pid}")
            seen_on[pid].add(r.id)
            totals[pid] += line.amount_cents
            if line.quantity_milli > 0 and line.amount_cents > 0:
                basis = "kg" if line.unit == "kg" else "pack"
                unit_prices[pid][basis].append(round(line.amount_cents * 1000 / line.quantity_milli))
            if pid not in last or (r.on, r.id) > (last[pid][0], last[pid][1]):
                last[pid] = (r.on, r.id, r.store_name)

    stats = []
    for pid, receipt_ids in seen_on.items():
        by_basis = unit_prices[pid]
        basis = max(by_basis, key=lambda b: len(by_basis[b])) if by_basis else "pack"
        prices = by_basis.get(basis)
        stats.append(
            ProductStat(
                product_id=pid, name=names[pid], times_bought=len(receipt_ids), total_cents=totals[pid],
                last_on=last[pid][0], last_store=last[pid][2],
                typical_price_cents=round(median(prices)) if prices else None, basis=basis,
            )
        )
    stats.sort(key=lambda s: (-s.times_bought, -s.total_cents, s.name.casefold()))
    return stats[:limit], unmatched
