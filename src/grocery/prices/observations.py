"""The price history: every receipt line and every shelf label becomes an observation."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, object_session

from grocery.db.models import PriceObservation, Product, Receipt, ShelfCapture
from grocery.products.content import unit_price_from_content


def comparable(price_cents: int, unit_price_cents: int | None, unit_basis: str | None) -> tuple[str, int]:
    """(basis, value) used to compare two observations: per kg/l when both have it, else per pack."""
    if unit_price_cents is not None and unit_basis in ("kg", "l"):
        return unit_basis, unit_price_cents
    return "pack", price_cents


def _per_content(product: Product | None, price_cents: int) -> tuple[int, str] | None:
    """(price per kg or l, basis) for a pack price, when the product's pack content is known."""
    if product is None or not product.pack_content or product.pack_basis not in ("kg", "l"):
        return None
    return unit_price_from_content(price_cents, product.pack_content), product.pack_basis


def set_pack_content(db: Session, product: Product, amount: int, basis: str) -> None:
    """Remember the pack content and give earlier pack prices of this product a price per kg or l."""
    product.pack_content, product.pack_basis, product.sold_per_piece = amount, basis, False
    db.flush()
    for obs in db.scalars(
        select(PriceObservation).where(
            PriceObservation.product_id == product.id, PriceObservation.unit_price_cents.is_(None)
        )
    ):
        obs.unit_price_cents, obs.unit_basis = _per_content(product, obs.price_cents)


def record_receipt(db: Session, receipt: Receipt) -> int:
    """Replace this receipt's observations with its current product lines."""
    db.execute(
        delete(PriceObservation).where(
            PriceObservation.source == "receipt", PriceObservation.source_ref_id == receipt.id
        )
    )
    if receipt.store_id is None or receipt.purchased_on is None:
        return 0
    count = 0
    for line in receipt.lines:
        if line.kind != "item" or line.product_id is None or line.line_total_cents <= 0:
            continue
        weighed = line.unit in ("kg", "l") and line.unit_price_cents is not None
        if weighed:
            price, unit_price, basis = line.line_total_cents, line.unit_price_cents, line.unit
        elif line.unit_price_cents is not None:
            price, unit_price, basis = line.unit_price_cents, None, None
        elif line.quantity_milli > 0:
            price, unit_price, basis = round(line.line_total_cents * 1000 / line.quantity_milli), None, None
        else:
            continue
        if not weighed:  # a pack price: with a known pack content it also has a price per kg or l
            unit_price, basis = _per_content(line.product, price) or (None, None)
        db.add(
            PriceObservation(
                product_id=line.product_id,
                store_id=receipt.store_id,
                price_cents=price,
                unit_price_cents=unit_price,
                unit_basis=basis,
                observed_on=receipt.purchased_on,
                source="receipt",
                source_ref_id=receipt.id,
            )
        )
        count += 1
    return count


def delete_receipt_observations(db: Session, receipt_id: int) -> None:
    db.execute(
        delete(PriceObservation).where(
            PriceObservation.source == "receipt", PriceObservation.source_ref_id == receipt_id
        )
    )


def _product_of(capture: ShelfCapture) -> Product | None:
    session = object_session(capture)
    return session.get(Product, capture.product_id) if session is not None and capture.product_id else None


def record_shelf(db: Session, capture: ShelfCapture, observed_on) -> PriceObservation | None:
    """The price that applies at the shelf: the promotion price when there is one."""
    price = capture.effective_price_cents if capture.effective_price_cents is not None else capture.price_cents
    if price is None or capture.store_id is None:
        return None
    db.execute(
        delete(PriceObservation).where(
            PriceObservation.source == "shelf", PriceObservation.source_ref_id == capture.id
        )
    )
    unit_price, basis = capture.unit_price_cents, capture.unit_basis
    if capture.effective_price_cents is not None and capture.effective_price_cents != capture.price_cents:
        # the printed unit price belongs to the normal price, not the promotion
        unit_price, basis = _per_content(_product_of(capture), price) or (None, None)
    obs = PriceObservation(
        product_id=capture.product_id,
        ean=capture.ean,
        store_id=capture.store_id,
        price_cents=price,
        unit_price_cents=unit_price,
        unit_basis=basis,
        is_promo=bool(capture.promo_text) or capture.effective_price_cents is not None,
        promo_text=capture.promo_text,
        requires_card=capture.requires_card,
        observed_on=observed_on,
        valid_until=capture.valid_until,
        source="shelf",
        source_ref_id=capture.id,
    )
    db.add(obs)
    return obs


def shelf_comparable(capture: ShelfCapture) -> tuple[str, int] | None:
    """The (basis, value) a saved shelf capture is compared on, or None when it has no price."""
    price = capture.effective_price_cents if capture.effective_price_cents is not None else capture.price_cents
    if price is None:
        return None
    unit_price, basis = capture.unit_price_cents, capture.unit_basis
    if capture.effective_price_cents is not None and capture.effective_price_cents != capture.price_cents:
        unit_price, basis = _per_content(_product_of(capture), price) or (None, None)
    return comparable(price, unit_price, basis)
