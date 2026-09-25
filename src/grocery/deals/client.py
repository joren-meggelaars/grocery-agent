"""PrijsProfeet client: a small, polite reader of their public offers API.

Their terms (https://www.prijsprofeet.nl/api-voorwaarden, v1.6, read 2026-09-23) allow free use without a key, also
for a private tool, on these conditions that shape this module:
- PrijsProfeet is named as the source wherever the data is shown (Deals page, notifications).
- A price shown as current is at most 24 hours old: the offers are refreshed nightly and expired ones are hidden.
- The catalogue is not walked to copy it. We only ask about products you buy and a few categories.
- Rate limits without a key: 30 requests/min on list endpoints. Calls are spaced MIN_INTERVAL apart.
- A user agent containing "bot", "crawler" or "spider" is blocked, so the default one avoids those words.
"""

import http.client
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

HOST = "www.prijsprofeet.nl"
SEARCH_PATH = "/api/v1/search"
MAX_BYTES = 2_000_000
MIN_INTERVAL = 3.5  # seconds between calls: about 17 a minute, well under the 30 a minute list limit
TIMEOUT = 10.0

# the retailer slugs of the source that we follow, mapped to our store chain (stores.chain)
RETAILERS = {"jumbo": "jumbo", "plus": "plus", "aldi": "aldi", "lidl": "lidl"}

CATEGORY_SLUGS = (
    "groente-fruit", "zuivel-eieren", "vega", "kaas", "vlees", "vis", "brood-bakkerij", "ontbijt",
    "pasta-rijst-wereldkeuken", "soepen-conserven-sauzen", "snoep-koek-chips", "frisdrank", "koffie-thee",
    "bier-wijn-sterke-drank", "diepvries", "huishouden", "drogisterij", "overig",
)

# (path, query parameters, user agent, api key) -> (http status, body); replaced in tests
Fetcher = Callable[[str, list[tuple[str, str]], str, str | None], tuple[int, bytes]]


class SourceError(Exception):
    """The source could not be read. stop=True: do not ask again this run (blocked, rate limited, down)."""

    def __init__(self, message: str, stop: bool = False) -> None:
        super().__init__(message)
        self.stop = stop


def http_get(path: str, params: list[tuple[str, str]], user_agent: str, api_key: str | None) -> tuple[int, bytes]:
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    conn = http.client.HTTPSConnection(HOST, timeout=TIMEOUT)  # fixed host: nothing user-controlled picks where we connect
    try:
        conn.request("GET", f"{path}?{urlencode(params)}", headers=headers)
        response = conn.getresponse()
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise SourceError("The answer was too large.", stop=True)
        return response.status, body
    finally:
        conn.close()


@dataclass(frozen=True)
class Offer:
    external_id: str
    retailer: str  # our chain: jumbo | plus | aldi | lidl
    name: str
    brand: str | None
    ean: str | None
    category: str | None
    private_label: bool | None  # the shop's own brand
    price_cents: int  # one item
    original_price_cents: int | None
    savings_pct: float | None
    savings_cents: int | None  # per item
    promo_type: str | None
    promo_text: str | None
    buy_quantity: int | None
    bundle_price_cents: int | None
    unit_price_cents: int | None
    unit_basis: str | None
    quantity_text: str | None
    loyalty_price_cents: int | None
    loyalty_program: str | None
    in_store_only: bool | None
    product_url: str | None
    valid_from: date | None
    valid_until: date | None


def _text(value, limit: int) -> str | None:
    return " ".join(value.split())[:limit] if isinstance(value, str) and value.strip() else None


def _cents(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return round(value * 100)


def _day(value) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None
    except ValueError:
        return None


def _safe_url(value) -> str | None:
    """Only http(s) links are ever shown as links."""
    return value[:500] if isinstance(value, str) and re.match(r"^https?://", value) else None


def parse_offer(raw) -> Offer | None:
    """One result of the source as an Offer, or None when it is not a usable current promotion."""
    if not isinstance(raw, dict):
        return None
    retailer = RETAILERS.get(raw.get("retailer"))
    external_id = _text(raw.get("product_id"), 80)
    name = _text(raw.get("name"), 300)
    price = raw.get("price")
    if retailer is None or not external_id or not name or not raw.get("is_promotional", True):
        return None
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        return None
    quantity = raw.get("multi_buy_quantity") if isinstance(raw.get("multi_buy_quantity"), int) else None
    if retailer == "aldi" and quantity and quantity > 1:
        price = price / quantity  # Aldi publishes the bundle total as the price
    price_cents = round(price * 100)
    original = _cents(raw.get("original_price"))
    if original is not None and original <= price_cents:
        original = None
    savings_pct = raw.get("savings_percentage")
    if isinstance(savings_pct, bool) or not isinstance(savings_pct, (int, float)):
        savings_pct = None
    if savings_pct is None and original:
        savings_pct = (original - price_cents) / original * 100
    savings_cents = original - price_cents if original else _cents(raw.get("savings_amount"))
    unit = raw.get("unit") if raw.get("unit") in ("kg", "l") else None
    unit_price = _cents(raw.get("unit_price")) if unit else None
    keywords = raw.get("promotional_keywords")
    promo_text = _text(", ".join(k for k in keywords if isinstance(k, str)), 200) if isinstance(keywords, list) else None
    bundle = _cents(raw.get("multi_buy_price"))
    if not promo_text and quantity and bundle:
        promo_text = f"{quantity} for EUR {bundle / 100:.2f}"
    loyalty = _cents(raw.get("loyalty_price"))
    ean = raw.get("ean")
    return Offer(
        external_id=external_id, retailer=retailer, name=name, brand=_text(raw.get("brand"), 100),
        ean=ean if isinstance(ean, str) and ean.isascii() and ean.isdigit() and len(ean) <= 14 else None,
        category=_text(raw.get("unified_category"), 40),
        private_label=raw.get("private_label") if isinstance(raw.get("private_label"), bool) else None,
        price_cents=price_cents, original_price_cents=original,
        savings_pct=float(savings_pct) if savings_pct is not None else None, savings_cents=savings_cents,
        promo_type=_text(raw.get("promotion_type"), 20), promo_text=promo_text,
        buy_quantity=quantity if quantity and quantity > 1 else None, bundle_price_cents=bundle,
        unit_price_cents=unit_price, unit_basis=unit if unit_price else None,
        quantity_text=_text(raw.get("quantity"), 60), loyalty_price_cents=loyalty,
        loyalty_program=_text(raw.get("loyalty_program"), 40) if loyalty else None,
        in_store_only=raw.get("in_store_only") if isinstance(raw.get("in_store_only"), bool) else None,
        product_url=_safe_url(raw.get("product_url")), valid_from=_day(raw.get("valid_from")),
        valid_until=_day(raw.get("valid_until")),
    )


class PrijsProfeet:
    def __init__(
        self, user_agent: str, api_key: str | None = None, fetch: Fetcher = http_get,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ua, self._key, self._fetch, self._sleep, self._clock = user_agent, api_key, fetch, sleep, clock
        self._last: float | None = None
        self.calls = 0

    def _pace(self) -> None:
        if self._last is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def search(self, params: list[tuple[str, str]]) -> list[Offer]:
        """Active offers for the given filters. Bad or unexpected answers become SourceError."""
        self._pace()
        self.calls += 1
        try:
            status, body = self._fetch(SEARCH_PATH, params, self._ua, self._key)
        except SourceError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise SourceError(f"The source could not be reached: {type(exc).__name__}", stop=True) from exc
        if status in (403, 429) or status >= 500:
            raise SourceError(f"The source answered {status}.", stop=True)
        if status != 200:
            raise SourceError(f"The source answered {status} for this question.")
        try:
            results = json.loads(body)["results"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SourceError("The source answered in an unexpected shape.", stop=True) from exc
        if not isinstance(results, list):
            raise SourceError("The source answered in an unexpected shape.", stop=True)
        return [o for o in map(parse_offer, results) if o is not None]
