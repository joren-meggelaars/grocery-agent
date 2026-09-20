"""Raw receipt text -> canonical product.

1. an exact learned mapping (chain + normalised text) wins outright;
2. otherwise the closest known mapping, same chain first, other chains only when very close;
3. otherwise the model's suggested name, matched against existing product names;
4. otherwise a new product is proposed from the suggested name (created only when confirmed).
"""

from dataclasses import dataclass

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.db.models import NameMapping, Product
from grocery.products.normalize import normalize_raw, product_key

SAME_CHAIN_CUTOFF = 88
OTHER_CHAIN_CUTOFF = 93
NAME_CUTOFF = 92


@dataclass(frozen=True)
class Match:
    source: str  # mapping | fuzzy | none
    product_id: int | None
    product_name: str | None  # existing product, or the proposed new name when source == none
    category_id: int | None
    confidence: float | None


class MatchIndex:
    """Loads mappings and products once per receipt so each line is a cheap in-memory lookup."""

    def __init__(self, db: Session) -> None:
        self._mappings = db.scalars(select(NameMapping)).all()
        self._products = db.scalars(select(Product)).all()
        self._exact = {(m.chain, m.raw_norm): m for m in self._mappings}
        self._by_key = {p.name_key: p for p in self._products}

    def find(self, chain: str, raw_text: str, suggested_name: str | None) -> Match:
        key = normalize_raw(raw_text)

        hit = self._exact.get((chain, key)) if key else None
        if hit is not None:
            return self._match("mapping", hit.product, 1.0)

        if key:
            same = [m for m in self._mappings if m.chain == chain]
            best = self._closest(key, same, SAME_CHAIN_CUTOFF)
            if best is None:
                best = self._closest(key, [m for m in self._mappings if m.chain != chain], OTHER_CHAIN_CUTOFF)
            if best is not None:
                mapping, score = best
                return self._match("fuzzy", mapping.product, round(score / 100, 2))

        if suggested_name and suggested_name.strip():
            exact = self._by_key.get(product_key(suggested_name))
            if exact is not None:
                return self._match("fuzzy", exact, 0.9)
            if self._products:
                names = {p.name_key: p for p in self._products}
                found = process.extractOne(
                    product_key(suggested_name), list(names), scorer=fuzz.token_set_ratio, score_cutoff=NAME_CUTOFF
                )
                if found:
                    return self._match("fuzzy", names[found[0]], round(found[1] / 100, 2))
            return Match("none", None, suggested_name.strip(), None, None)

        return Match("none", None, None, None, None)

    @staticmethod
    def _closest(key: str, mappings: list[NameMapping], cutoff: int):
        if not mappings:
            return None
        by_norm = {m.raw_norm: m for m in mappings}
        found = process.extractOne(key, list(by_norm), scorer=fuzz.token_set_ratio, score_cutoff=cutoff)
        return (by_norm[found[0]], found[1]) if found else None

    @staticmethod
    def _match(source: str, product: Product, confidence: float) -> Match:
        return Match(source, product.id, product.name, product.category_id, confidence)
