"""Loads the plain facts the aggregations work on: confirmed, dated receipts only."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from grocery.analytics.aggregate import CategoryMeta, LineFact, ReceiptFact
from grocery.db.models import Category, Receipt, ReceiptLine


def load_categories(db: Session) -> dict[int, CategoryMeta]:
    return {c.id: CategoryMeta(c.id, c.name, c.counts_as_food) for c in db.scalars(select(Category))}


def load_receipts(db: Session, since: date | None = None, until: date | None = None) -> list[ReceiptFact]:
    """Confirmed receipts with a date, purchase date within [since, until)."""
    stmt = (
        select(Receipt)
        .where(Receipt.status == "confirmed", Receipt.purchased_on.is_not(None))
        .options(joinedload(Receipt.store), selectinload(Receipt.lines).joinedload(ReceiptLine.product))
        .order_by(Receipt.purchased_on, Receipt.id)
    )
    if since is not None:
        stmt = stmt.where(Receipt.purchased_on >= since)
    if until is not None:
        stmt = stmt.where(Receipt.purchased_on < until)
    facts = []
    for r in db.scalars(stmt).unique():
        lines = tuple(
            LineFact(
                kind=l.kind,
                amount_cents=l.line_total_cents,
                category_id=l.category_id,
                product_id=l.product_id,
                product_name=l.product.name if l.product else None,
                quantity_milli=l.quantity_milli,
                unit=l.unit,
            )
            for l in r.lines
        )
        facts.append(
            ReceiptFact(
                id=r.id,
                on=r.purchased_on,
                store_name=r.store.name if r.store else "Unknown store",
                total_cents=r.total_cents or 0,
                lines=lines,
            )
        )
    return facts
