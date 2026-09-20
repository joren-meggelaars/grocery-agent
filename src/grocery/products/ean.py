"""Barcode (EAN/UPC) validation with the GS1 check digit, so a mis-scan or a typo never becomes a product."""

import re

VALID_LENGTHS = (8, 12, 13, 14)
_JUNK = re.compile(r"[\s\-]")


def normalize_ean(text: str | None) -> str | None:
    """Digits only, or None if the text is not a barcode number at all."""
    if not text:
        return None
    digits = _JUNK.sub("", text)
    # isascii(): str.isdigit() also accepts other scripts' digits, which must never reach a URL
    return digits if digits.isascii() and digits.isdigit() and len(digits) in VALID_LENGTHS else None


def check_digit_ok(digits: str) -> bool:
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def clean_ean(text: str | None) -> str | None:
    """A valid barcode number (checksum verified) or None."""
    digits = normalize_ean(text)
    return digits if digits is not None and check_digit_ok(digits) else None
