"""Confirm form for a shelf capture -> CaptureInput (plus the typed values for re-rendering on errors)."""

from datetime import date

from grocery.capture.service import PROMO_KINDS, CaptureInput
from grocery.db.models import ShelfCapture
from grocery.money import ParseError, format_cents, parse_euro


def values_from_capture(capture: ShelfCapture, default_store: str) -> dict:
    return {
        "ean": capture.ean or "",
        "product": capture.product_name or capture.off_name or "",
        "category_id": capture.category_id,
        "store": capture.store.chain if capture.store else default_store,
        "price": format_cents(capture.price_cents),
        "effective_price": format_cents(capture.effective_price_cents),
        "unit_price": format_cents(capture.unit_price_cents),
        "unit_basis": capture.unit_basis or "kg",
        "promo_kind": capture.promo_kind or "",
        "promo_text": capture.promo_text or "",
        "requires_card": capture.requires_card,
        "valid_from": capture.valid_from.isoformat() if capture.valid_from else "",
        "valid_until": capture.valid_until.isoformat() if capture.valid_until else "",
    }


def parse_capture_form(form) -> tuple[CaptureInput, dict, list[str]]:
    errors: list[str] = []
    get = lambda k: (form.get(k) or "").strip()  # noqa: E731

    def money(name: str, label: str) -> int | None:
        if not get(name):
            return None
        try:
            return parse_euro(get(name))
        except ParseError:
            errors.append(f"{label} is not a valid amount.")
            return None

    def day(name: str, label: str) -> date | None:
        if not get(name):
            return None
        try:
            return date.fromisoformat(get(name))
        except ValueError:
            errors.append(f"{label} is not a valid date.")
            return None

    category = get("category")
    promo_kind = get("promo_kind")
    data = CaptureInput(
        ean_text=get("ean"),
        product_name=get("product"),
        category_id=int(category) if category.isdigit() else None,
        store_chain=get("store"),
        price_cents=money("price", "The shelf price"),
        effective_price_cents=money("effective_price", "The promotion price"),
        unit_price_cents=money("unit_price", "The price per unit"),
        unit_basis=get("unit_basis") or None,
        promo_kind=promo_kind if promo_kind in PROMO_KINDS else None,
        promo_text=get("promo_text"),
        requires_card=bool(form.get("requires_card")),
        valid_from=day("valid_from", "The start date"),
        valid_until=day("valid_until", "The end date"),
    )
    values = {
        "ean": get("ean"), "product": get("product"), "category_id": data.category_id, "store": get("store"),
        "price": get("price"), "effective_price": get("effective_price"), "unit_price": get("unit_price"),
        "unit_basis": get("unit_basis") or "kg", "promo_kind": get("promo_kind"), "promo_text": get("promo_text"),
        "requires_card": data.requires_card, "valid_from": get("valid_from"), "valid_until": get("valid_until"),
    }
    return data, values, errors
