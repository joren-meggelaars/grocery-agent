"""Euro amounts as integer cents, and quantities as integer thousandths."""

import re

_AMOUNT = re.compile(r"^-?\d+(\.\d{1,2})?$")


class ParseError(ValueError):
    pass


def parse_euro(text: str) -> int:
    """'1,29' / '1.29' / '€ 1,29' / '-0,50' / '12' -> cents. Raises ParseError otherwise."""
    s = text.strip().replace("€", "").replace(" ", "").replace(" ", "")
    if not s:
        raise ParseError("empty amount")
    if "," in s and "." in s:
        # the later separator is the decimal one; the other is a thousands separator
        thousands = "." if s.rfind(",") > s.rfind(".") else ","
        s = s.replace(thousands, "")
    s = s.replace(",", ".")
    if not _AMOUNT.match(s):
        raise ParseError(f"not an amount: {text!r}")
    negative = s.startswith("-")
    whole, _, frac = s.lstrip("-").partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2]) if frac else int(whole) * 100
    return -cents if negative else cents


def format_cents(cents: int | None) -> str:
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def parse_quantity_milli(text: str) -> int:
    """'2' -> 2000, '0,874' -> 874."""
    s = text.strip().replace(",", ".")
    if not re.match(r"^\d+(\.\d{1,3})?$", s):
        raise ParseError(f"not a quantity: {text!r}")
    whole, _, frac = s.partition(".")
    return int(whole) * 1000 + int((frac + "000")[:3])


def format_quantity(milli: int) -> str:
    if milli % 1000 == 0:
        return str(milli // 1000)
    return f"{milli / 1000:.3f}".rstrip("0")
