"""Does the receipt add up? Pure functions, no database.

Every line contributes its line_total_cents (discounts are negative, deposit refunds negative),
so the receipt total must equal the plain sum of all lines, to the cent.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

OLD_DATE_DAYS = 400


@dataclass(frozen=True)
class LineData:
    line_no: int
    kind: str
    quantity_milli: int
    unit_price_cents: int | None
    line_total_cents: int


@dataclass
class ValidationResult:
    delta_cents: int | None  # total - sum(lines); 0 means it adds up. None if there is no total.
    flags: list[str] = field(default_factory=list)
    line_flags: dict[int, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.flags and not self.line_flags


def expected_line_total(quantity_milli: int, unit_price_cents: int) -> int:
    """quantity x unit price, rounded half up to whole cents."""
    return (quantity_milli * unit_price_cents + 500) // 1000


def validate_receipt(
    lines: list[LineData],
    total_cents: int | None,
    purchased_on: date | None,
    today: date | None = None,
) -> ValidationResult:
    today = today or date.today()
    result = ValidationResult(delta_cents=None)

    if not lines:
        result.flags.append("no_lines")

    if total_cents is None:
        result.flags.append("no_total")
    else:
        result.delta_cents = total_cents - sum(l.line_total_cents for l in lines)
        if result.delta_cents != 0:
            result.flags.append("sum_mismatch")

    for line in lines:
        problems = []
        if line.kind == "item" and line.unit_price_cents is not None:
            expected = expected_line_total(line.quantity_milli, line.unit_price_cents)
            if abs(expected - line.line_total_cents) > 1:
                problems.append("line_math")
        if line.kind == "item" and line.line_total_cents < 0:
            problems.append("negative_item")
        if line.kind == "discount" and line.line_total_cents > 0:
            problems.append("positive_discount")
        if problems:
            result.line_flags[line.line_no] = problems

    if purchased_on is None:
        result.flags.append("no_date")
    elif purchased_on > today:
        result.flags.append("date_future")
    elif purchased_on < today - timedelta(days=OLD_DATE_DAYS):
        result.flags.append("date_old")

    return result


FLAG_MESSAGES = {
    "no_lines": "No line items were found.",
    "no_total": "No total was found.",
    "sum_mismatch": "The lines do not add up to the total.",
    "no_date": "No purchase date was found.",
    "date_future": "The date is in the future.",
    "date_old": "The date is more than a year ago.",
    "possible_duplicate": "A receipt with the same store, date and total already exists.",
    "duplicate_photo": "This photo was uploaded before.",
    "line_math": "Quantity x price does not match the line total.",
    "negative_item": "A product line has a negative amount.",
    "positive_discount": "A discount line has a positive amount.",
}
