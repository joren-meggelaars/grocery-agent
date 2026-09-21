"""Pack content ("500 g", "1,5 l", "6 x 33 cl") and the price per kg or l that follows from it.

Prices that were read as a price per pack can only be compared with a price per kg or l when the
content of the pack is known. Stored on the product, in grams or millilitres.
"""

import re

MAX_CONTENT = 100_000  # 100 kg or 100 l: beyond that it is a typo

_UNITS = {  # unit -> (basis, grams or millilitres per unit)
    "g": ("kg", 1), "gr": ("kg", 1), "gram": ("kg", 1), "kg": ("kg", 1000), "kilo": ("kg", 1000),
    "ml": ("l", 1), "cl": ("l", 10), "dl": ("l", 100), "l": ("l", 1000), "lt": ("l", 1000),
    "liter": ("l", 1000), "litre": ("l", 1000),
}
_PATTERN = re.compile(r"^(?:(\d{1,3})\s*[x*]\s*)?(\d+(?:[.,]\d{1,3})?)\s*([a-z]+)$")


class ContentError(ValueError):
    pass


def parse_content(text: str) -> tuple[int, str]:
    """(amount in g or ml, basis 'kg' or 'l'). Accepts '500g', '0,5 kg', '33 cl', '6 x 33 cl'."""
    match = _PATTERN.match(text.strip().casefold())
    if match is None or match.group(3) not in _UNITS:
        raise ContentError("Enter the content like 500 g, 1,5 l or 6 x 33 cl.")
    count, number, unit = match.groups()
    basis, factor = _UNITS[unit]
    amount = round(float(number.replace(",", ".")) * factor * int(count or 1))
    if not 0 < amount <= MAX_CONTENT:
        raise ContentError("That content is not realistic: check the number and the unit.")
    return amount, basis


def format_content(amount: int | None, basis: str | None) -> str:
    if not amount or basis not in ("kg", "l"):
        return ""
    if amount >= 1000:
        return f"{amount / 1000:g} {basis}".replace(".", ",")
    return f"{amount} {'g' if basis == 'kg' else 'ml'}"


def unit_price_from_content(price_cents: int, amount: int) -> int:
    """Price per kg or l for a pack of `amount` g or ml, rounded half up to whole cents."""
    return (price_cents * 2000 + amount) // (2 * amount)


# Categories and words for products that are plainly sold per piece: no question about their weight.
PIECE_CATEGORIES = {"Brood & banket", "Huishouden & verzorging"}
_PIECE_WORDS = re.compile(
    r"\b(ei|eieren|komkommer|sla|kropsla|ijsbergsla|bloemkool|spitskool|ananas|meloen|avocado|citroen|limoen|"
    r"brood|broodje|broodjes|stokbrood|croissant|wrap|wraps|pizza|batterij|batterijen|toiletpapier|keukenpapier|"
    r"tandpasta|shampoo|zeep|bloemen|plant|plantje|kaars|tijdschrift|krant)\b"
)


def is_sold_per_piece(name: str, category_name: str | None, flagged: bool = False) -> bool:
    if flagged or category_name in PIECE_CATEGORIES:
        return True
    return _PIECE_WORDS.search(name.casefold()) is not None
