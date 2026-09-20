"""The price history: every receipt line and every shelf label becomes an observation."""

from sqlalchemy import delete
from sqlalchemy.orm import Session

from grocery.db.models import PriceObservation, Receipt, ShelfCapture


def comparable(price_cents: int, unit_price_cents: int | None, unit_basis: str | None) -> tuple[str, int]:
    """(basis, value) used to compare two observations: per kg/l when both have it, else per pack."""
    if unit_price_cents is not None and unit_basis in ("kg", "l"):
        return unit_basis, unit_price_cents
    return "pack", price_cents


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
        unit_price, basis = None, None  # the printed unit price belongs to the normal price, not the promotion
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
        unit_price, basis = None, None
    return comparable(price, unit_price, basis)
