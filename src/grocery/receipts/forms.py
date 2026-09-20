"""Turns the review form (dynamic line rows) into a ConfirmInput, and back into view rows."""

from datetime import date

from grocery.money import ParseError, format_cents, format_quantity, parse_euro, parse_quantity_milli
from grocery.receipts.service import ConfirmInput, LineInput
from grocery.receipts.validation import ValidationResult

KINDS = ("item", "discount", "deposit", "bag", "rounding")
UNITS = ("pcs", "kg", "l")
MAX_LINES = 200


def _blank_row(index: int) -> dict:
    return {
        "i": index, "orig": "", "kind": "item", "raw": "", "qty": "1", "unit": "pcs", "unit_price": "",
        "total": "", "product": "", "category_id": None, "match_source": "none", "match_conf": None,
        "flags": [],
    }


def blank_row(index: int = 0) -> dict:
    return _blank_row(index)


def rows_from_receipt(receipt, validation: ValidationResult | None = None) -> list[dict]:
    rows = []
    for index, l in enumerate(receipt.lines):
        row = _blank_row(index)
        row.update(
            orig=str(l.line_no), kind=l.kind, raw=l.raw_text, qty=format_quantity(l.quantity_milli),
            unit=l.unit, unit_price=format_cents(l.unit_price_cents), total=format_cents(l.line_total_cents),
            product=(l.product.name if l.product else l.suggested_name) or "", category_id=l.category_id,
            match_source=l.match_source, match_conf=l.match_confidence,
            flags=validation.line_flags.get(l.line_no, []) if validation else [],
        )
        rows.append(row)
    return rows


def parse_confirm_form(form) -> tuple[ConfirmInput, list[dict], list[str]]:
    """Returns (input, rows as submitted, errors). Rows let the page re-render exactly what was typed."""
    errors: list[str] = []
    rows: list[dict] = []
    lines: list[LineInput] = []

    try:
        count = min(int(form.get("line_count", "0")), MAX_LINES)
    except ValueError:
        count = 0

    for i in range(count):
        get = lambda name, default="": (form.get(f"l{i}_{name}", default) or default).strip()  # noqa: E731
        row = _blank_row(len(rows))
        row.update(
            orig=get("orig"), kind=get("kind", "item"), raw=get("raw"), qty=get("qty", "1") or "1",
            unit=get("unit", "pcs"), unit_price=get("unit_price"), total=get("total"), product=get("product"),
            category_id=int(get("category")) if get("category").isdigit() else None,
        )
        if form.get(f"l{i}_delete") or (not row["raw"] and not row["total"]):
            continue  # removed, or an untouched empty row
        rows.append(row)
        label = f"Line {len(rows)}"
        try:
            if row["kind"] not in KINDS or row["unit"] not in UNITS:
                raise ParseError("bad kind or unit")
            total = parse_euro(row["total"])
            unit_price = parse_euro(row["unit_price"]) if row["unit_price"] else None
            quantity = parse_quantity_milli(row["qty"])
        except ParseError:
            errors.append(f"{label}: check the amount, price and quantity.")
            continue
        if not row["raw"]:
            errors.append(f"{label}: the receipt text is empty.")
            continue
        lines.append(
            LineInput(
                orig_no=int(row["orig"]) if row["orig"].isdigit() else None,
                kind=row["kind"], raw_text=row["raw"], quantity_milli=quantity, unit=row["unit"],
                unit_price_cents=unit_price, line_total_cents=total, product_name=row["product"],
                category_id=row["category_id"],
            )
        )

    purchased_on = None
    raw_date = (form.get("purchased_on") or "").strip()
    if raw_date:
        try:
            purchased_on = date.fromisoformat(raw_date)
        except ValueError:
            errors.append("The date is not valid.")
    total_cents = None
    raw_total = (form.get("total") or "").strip()
    if raw_total:
        try:
            total_cents = parse_euro(raw_total)
        except ParseError:
            errors.append("The receipt total is not a valid amount.")

    data = ConfirmInput(
        store_chain=(form.get("store") or "other").strip(),
        purchased_on=purchased_on,
        total_cents=total_cents,
        lines=lines,
        accept_mismatch=bool(form.get("accept_mismatch")),
    )
    return data, rows, errors
