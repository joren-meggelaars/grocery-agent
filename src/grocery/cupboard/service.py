"""The cupboard: a one-off inventory of products at home, to decide what to compare at cheaper shops.

Scan barcodes in one go. A barcode the app knows is added at once; an unknown one waits in a list (the worker
looks up its name at Open Food Facts) and is named afterwards, all in one sitting. No quantities, no expiry.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grocery.db.base import utcnow
from grocery.db.models import (
    Category,
    CupboardItem,
    CupboardScan,
    PriceObservation,
    ProductEan,
    Receipt,
    ReceiptLine,
)
from grocery.jobs.queue import enqueue
from grocery.prices.feedback import Seen, feedback_for, seen_from
from grocery.prices.observations import comparable
from grocery.products.ean import clean_ean
from grocery.products.matching import MatchIndex
from grocery.receipts.service import ConfirmError, get_or_create_product

log = logging.getLogger(__name__)

FREQUENT_DAYS = 90


class InvalidBarcode(ValueError):
    pass


@dataclass(frozen=True)
class ScanOutcome:
    kind: str  # added | already | unknown | unknown_again
    name: str | None = None
    product_id: int | None = None
    scan_id: int | None = None


# --- scanning ---------------------------------------------------------------

def scan(db: Session, ean_text: str | None, now: datetime | None = None) -> ScanOutcome:
    now = now or utcnow()
    ean = clean_ean(ean_text)
    if ean is None:
        raise InvalidBarcode("That barcode number is not valid.")

    link = db.get(ProductEan, ean)
    if link is not None:
        item = db.scalar(select(CupboardItem).where(CupboardItem.product_id == link.product_id))
        if item is None:
            db.add(CupboardItem(product_id=link.product_id, added_at=now, last_scanned_at=now))
            kind = "added"
        else:
            item.last_scanned_at = now
            kind = "already"
        db.commit()
        return ScanOutcome(kind, link.product.name, link.product_id)

    pending = db.scalar(select(CupboardScan).where(CupboardScan.ean == ean))
    if pending is not None:
        pending.seen_count += 1
        db.commit()
        return ScanOutcome("unknown_again", pending.off_name, scan_id=pending.id)
    pending = CupboardScan(ean=ean, status="lookup", created_at=now)
    db.add(pending)
    db.flush()
    enqueue(db, "lookup_ean", {"ean": ean})
    db.commit()
    return ScanOutcome("unknown", scan_id=pending.id)


def pending_scans(db: Session) -> list[CupboardScan]:
    return list(db.scalars(select(CupboardScan).order_by(CupboardScan.id)))


def suggestion(db: Session, pending: CupboardScan) -> tuple[str, int | None]:
    """Prefilled name and category: an existing product if the Open Food Facts name matches one, else that name."""
    if not pending.off_name:
        return "", None
    name = pending.off_name.split(",")[0].strip()  # the product part of "name, brand, quantity"
    match = MatchIndex(db).find("other", name, name)
    if match.source != "none":
        return match.product_name or name, match.category_id
    return name, None


def name_scan(
    db: Session, scan_id: int, product_name: str, category_id: int | None, heavy_use: bool, now: datetime | None = None
) -> CupboardItem:
    now = now or utcnow()
    pending = db.get(CupboardScan, scan_id)
    if pending is None:
        raise ConfirmError(["That barcode was already handled."])
    if not product_name.strip():
        raise ConfirmError(["Enter the product name."])
    product = get_or_create_product(db, product_name, category_id)
    link = db.get(ProductEan, pending.ean)
    if link is None:
        db.add(ProductEan(ean=pending.ean, product_id=product.id, source="scan"))
    elif link.product_id != product.id:
        link.product_id = product.id
    item = _ensure_item(db, product.id, heavy_use, now)
    db.delete(pending)
    db.commit()
    return item


def add_product(db: Session, product_name: str, category_id: int | None, heavy_use: bool, now: datetime | None = None) -> CupboardItem:
    """For things without a barcode (fresh produce, bakery): add by name."""
    if not product_name.strip():
        raise ConfirmError(["Enter the product name."])
    product = get_or_create_product(db, product_name, category_id)
    item = _ensure_item(db, product.id, heavy_use, now or utcnow())
    db.commit()
    return item


def _ensure_item(db: Session, product_id: int, heavy_use: bool, now: datetime) -> CupboardItem:
    item = db.scalar(select(CupboardItem).where(CupboardItem.product_id == product_id))
    if item is None:
        item = CupboardItem(product_id=product_id, heavy_use=heavy_use, added_at=now, last_scanned_at=now)
        db.add(item)
    else:
        item.heavy_use = item.heavy_use or heavy_use
        item.last_scanned_at = now
    db.flush()
    return item


def discard_scan(db: Session, scan_id: int) -> None:
    pending = db.get(CupboardScan, scan_id)
    if pending is not None:
        db.delete(pending)
        db.commit()


def set_heavy(db: Session, item_id: int, value: bool) -> None:
    item = db.get(CupboardItem, item_id)
    if item is not None:
        item.heavy_use = value
        db.commit()


def remove_item(db: Session, item_id: int) -> None:
    item = db.get(CupboardItem, item_id)
    if item is not None:
        db.delete(item)
        db.commit()


def on_cupboard(db: Session, product_id: int | None) -> bool:
    if product_id is None:
        return False
    return db.scalar(select(CupboardItem.id).where(CupboardItem.product_id == product_id)) is not None


# --- the list ---------------------------------------------------------------

@dataclass
class CupboardRow:
    item: CupboardItem
    name: str
    category: str | None
    heavy: bool
    times_bought: int  # receipts in the last 90 days
    last: Seen | None  # what was paid (latest receipt); else the latest shelf sighting
    paid: bool  # True when `last` comes from a receipt, i.e. it is what you actually paid
    basis: str | None
    cheaper: Seen | None  # the cheapest recent sighting at another store, if any


def cupboard_rows(db: Session, today: date) -> list[CupboardRow]:
    items = list(db.scalars(select(CupboardItem)))
    if not items:
        return []
    ids = [i.product_id for i in items]

    since = today - timedelta(days=FREQUENT_DAYS)
    bought = dict(
        db.execute(
            select(ReceiptLine.product_id, func.count(func.distinct(ReceiptLine.receipt_id)))
            .join(Receipt, Receipt.id == ReceiptLine.receipt_id)
            .where(
                Receipt.status == "confirmed",
                Receipt.purchased_on >= since,
                ReceiptLine.kind == "item",
                ReceiptLine.product_id.in_(ids),
            )
            .group_by(ReceiptLine.product_id)
        ).all()
    )

    latest_paid: dict[int, PriceObservation] = {}
    latest_seen: dict[int, PriceObservation] = {}
    for obs in db.scalars(
        select(PriceObservation)
        .where(PriceObservation.product_id.in_(ids))
        .order_by(PriceObservation.observed_on.desc(), PriceObservation.id.desc())
    ):
        latest_seen.setdefault(obs.product_id, obs)
        if obs.source == "receipt":
            latest_paid.setdefault(obs.product_id, obs)

    rows = []
    for item in items:
        paid = item.product_id in latest_paid
        obs = latest_paid.get(item.product_id) or latest_seen.get(item.product_id)
        last = cheaper = basis = None
        if obs is not None:
            basis, value = comparable(obs.price_cents, obs.unit_price_cents, obs.unit_basis)
            last = seen_from(obs, value)
            fb = feedback_for(db, item.product_id, obs.store_id, basis, value, today)
            cheaper = fb.cheaper_elsewhere[0] if fb.cheaper_elsewhere else None
        category = item.product.category.name if item.product.category else None
        rows.append(
            CupboardRow(item, item.product.name, category, item.heavy_use, bought.get(item.product_id, 0), last, paid, basis, cheaper)
        )
    rows.sort(key=lambda r: (not r.heavy, -r.times_bought, r.name.casefold()))
    return rows


def categories(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).order_by(Category.sort_order)))
