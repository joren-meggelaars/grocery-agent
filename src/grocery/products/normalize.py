import re
import unicodedata

_AMOUNT = re.compile(r"\b\d+[.,]\d{2}\b")
_LEADING_QTY = re.compile(r"^\s*\d+\s*[x*]\s+", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")

MAX_KEY = 200


def normalize_raw(text: str) -> str:
    """Stable key for receipt text: case, accents, punctuation, prices and '2 x' prefixes ignored.

    'Halfvolle Melk 1L   1,29' and 'HALFVOLLE MELK 1L' map to the same key.
    """
    text = _LEADING_QTY.sub("", text)
    text = _AMOUNT.sub(" ", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    text = _NON_ALNUM.sub(" ", text)
    return _SPACES.sub(" ", text).strip()[:MAX_KEY]


def product_key(name: str) -> str:
    return _SPACES.sub(" ", name.strip()).casefold()[:MAX_KEY]
