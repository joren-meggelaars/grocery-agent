"""Shopping list: add products (by name, from another page, or by barcode), and per item, where to buy
it based on what you already know: an active deal for this exact product first, else the cheapest
price seen recently (your last store when nothing is cheaper elsewhere). Lidl is called out by name
when it is the answer, since it is your savings target.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from grocery.db.base import utcnow
from grocery.db.models import Deal, Product, ProductEan, ShoppingListItem, Store
from grocery.prices.feedback import price_overview
from grocery.products.ean import clean_ean
from grocery.products.matching import MatchIndex
from grocery.receipts.service import ConfirmError

LIDL_NAME = "Lidl"


class InvalidBarcode(ValueError):
    pass


@dataclass(frozen=True)
class Advice:
    kind: str  # deal | price
    store: str
    price_cents: int
    basis: str  # pack | kg | l
    note: str
    lidl_tip: bool
    source: str | None = None  # named where required by the source's terms (PrijsProfeet)


@dataclass
class Row:
    item: ShoppingListItem
    name: str
    advice: Advice | None


def _best_deal(db: Session, product_id: int, today: date) -> Deal | None:
    """The cheapest currently valid offer for this exact product, if any (not gated by the radar's alert
    thresholds: here you asked about this product yourself, so even a small known discount is useful)."""
    return db.scalar(
        select(Deal)
        .where(
            Deal.matched_product_id == product_id,
            (Deal.valid_until.is_(None)) | (Deal.valid_until >= today),
            (Deal.valid_from.is_(None)) | (Deal.valid_from <= today),
        )
        .order_by(Deal.price_cents)
        .limit(1)
    )


def build_advice(db: Session, product_ids: list[int], today: date) -> dict[int, Advice | None]:
    ids = [pid for pid in product_ids if pid]
    if not ids:
        return {}
    overview = price_overview(db, ids, today)
    store_names = {s.chain: s.name for s in db.scalars(select(Store))}
    out: dict[int, Advice | None] = {}
    for pid in ids:
        deal = _best_deal(db, pid, today)
        if deal is not None:
            store = store_names.get(deal.retailer, deal.retailer)
            out[pid] = Advice(
                "deal", store, deal.price_cents, deal.unit_basis or "pack",
                deal.reason or deal.promo_text or "current offer", store == LIDL_NAME, source="PrijsProfeet",
            )
            continue
        view = overview.get(pid)
        if view is None or view.last is None:
            out[pid] = None
            continue
        if view.alternative is not None:
            note = f"{view.pct * 100:+.0f}% vs {view.last.store}" if view.pct is not None else ""
            out[pid] = Advice("price", view.alternative.store, view.alternative.value, view.basis, note,
                               view.alternative.store == LIDL_NAME)
        else:
            note = "your usual price" if view.paid else "last seen at this price"
            out[pid] = Advice("price", view.last.store, view.last.value, view.basis, note,
                               view.last.store == LIDL_NAME)
    return out


def rows(db: Session, today: date) -> list[Row]:
    items = list(db.scalars(select(ShoppingListItem).order_by(ShoppingListItem.added_at)))
    advice = build_advice(db, [i.product_id for i in items], today)
    out = [
        Row(item, item.product.name if item.product else item.raw_name, advice.get(item.product_id))
        for item in items
    ]
    out.sort(key=lambda r: (r.item.bought_at is not None, r.item.added_at))
    return out


def pending_count(db: Session) -> int:
    return db.scalar(select(func.count(ShoppingListItem.id)).where(ShoppingListItem.bought_at.is_(None))) or 0


# --- adding -------------------------------------------------------------------------------------

def add_by_name(db: Session, text: str, note: str, now: datetime | None = None) -> ShoppingListItem:
    now = now or utcnow()
    name = " ".join(text.split())[:200]
    if not name:
        raise ConfirmError(["Enter a product name."])
    match = MatchIndex(db).find("", name, name)  # no receipt chain here: matched on the name alone
    item = ShoppingListItem(
        raw_name=name, note=(note or "").strip()[:200] or None, added_at=now,
        product_id=match.product_id if match.source != "none" else None,
    )
    db.add(item)
    db.commit()
    return item


def add_by_product(db: Session, product_id: int, now: datetime | None = None) -> ShoppingListItem:
    now = now or utcnow()
    product = db.get(Product, product_id)
    if product is None:
        raise ConfirmError(["Unknown product."])
    existing = db.scalar(
        select(ShoppingListItem).where(
            ShoppingListItem.product_id == product.id, ShoppingListItem.bought_at.is_(None)
        )
    )
    if existing is not None:
        return existing  # already on the list: no duplicate row
    item = ShoppingListItem(raw_name=product.name, product_id=product.id, added_at=now)
    db.add(item)
    db.commit()
    return item


def add_by_ean(db: Session, ean_text: str | None, now: datetime | None = None) -> ShoppingListItem:
    now = now or utcnow()
    ean = clean_ean(ean_text)
    if ean is None:
        raise InvalidBarcode("That barcode number is not valid.")
    link = db.get(ProductEan, ean)
    if link is None:
        raise InvalidBarcode("Unknown barcode. Add it by name, or scan it once in Products at home or in the shop first.")
    return add_by_product(db, link.product_id, now)


# --- changing --------------------------------------------------------------------------------

def toggle_bought(db: Session, item_id: int, now: datetime | None = None) -> None:
    item = db.get(ShoppingListItem, item_id)
    if item is None:
        return
    item.bought_at = None if item.bought_at is not None else (now or utcnow())
    db.commit()


def remove(db: Session, item_id: int) -> None:
    item = db.get(ShoppingListItem, item_id)
    if item is not None:
        db.delete(item)
        db.commit()


def clear_bought(db: Session) -> int:
    result = db.execute(delete(ShoppingListItem).where(ShoppingListItem.bought_at.is_not(None)))
    db.commit()
    return result.rowcount
