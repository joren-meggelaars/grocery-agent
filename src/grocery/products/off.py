"""Open Food Facts lookups, cached (including "not found").

Only one fixed host is ever contacted, the barcode is validated as digits before it reaches the URL,
redirects are not followed and the response size is capped. Data is ODbL: attribution is shown in the UI.
"""

import http.client
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from grocery.config import Settings
from grocery.db.base import utcnow
from grocery.db.models import OffCache
from grocery.products.ean import normalize_ean

log = logging.getLogger(__name__)

OFF_HOST = "world.openfoodfacts.org"
MAX_BYTES = 256 * 1024
FIELDS = "product_name,brands,quantity,generic_name"

# (ean, user_agent) -> (http status, body bytes); replaced in tests
Fetcher = Callable[[str, str], tuple[int, bytes]]


@dataclass(frozen=True)
class OffProduct:
    name: str | None
    brand: str | None
    quantity: str | None

    @property
    def display(self) -> str | None:
        parts = [p for p in (self.name, self.brand, self.quantity) if p]
        seen, out = set(), []
        for part in parts:
            if part.casefold() not in seen:
                seen.add(part.casefold())
                out.append(part)
        return ", ".join(out) or None


def http_fetch(ean: str, user_agent: str, timeout: float = 5.0) -> tuple[int, bytes]:
    if not (ean.isascii() and ean.isdigit()):
        raise ValueError("EAN must be ASCII digits")
    conn = http.client.HTTPSConnection(OFF_HOST, timeout=timeout)
    try:
        conn.request(
            "GET",
            f"/api/v2/product/{ean}.json?fields={FIELDS}",
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        response = conn.getresponse()
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("response too large")
        return response.status, body
    finally:
        conn.close()


def _clean(value) -> str | None:
    return " ".join(value.split())[:200] if isinstance(value, str) and value.strip() else None


def _parse(body: bytes) -> OffProduct | None:
    """The product, None for a well-formed "not found" answer; ValueError for anything unexpected."""
    data = json.loads(body)
    if not isinstance(data, dict) or "status" not in data:
        raise ValueError("unexpected response shape")
    product = data.get("product")
    if data["status"] != 1 or not isinstance(product, dict):
        return None
    name = _clean(product.get("product_name")) or _clean(product.get("generic_name"))
    brand = _clean((product.get("brands") or "").split(",")[0]) if product.get("brands") else None
    return OffProduct(name=name, brand=brand, quantity=_clean(product.get("quantity")))


def lookup(
    db: Session,
    settings: Settings,
    ean: str,
    fetch: Fetcher = http_fetch,
    now: datetime | None = None,
) -> OffProduct | None:
    """The product, or None when unknown or when Open Food Facts could not be reached."""
    ean = normalize_ean(ean) or ""
    if not ean:
        return None
    now = now or utcnow()
    row = db.get(OffCache, ean)
    if row is not None and row.expires_at > now:
        return OffProduct(**row.payload) if row.found and row.payload else None

    try:
        status, body = fetch(ean, settings.off_user_agent)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        log.warning("Open Food Facts unreachable for %s: %s", ean, exc)
        return None

    if status not in (200, 404):
        log.warning("Open Food Facts answered %s for %s; not cached", status, ean)
        return None
    product = None
    if status == 200:  # a 404 means "unknown" whatever the body says
        try:
            product = _parse(body)
        except ValueError:
            log.warning("Open Food Facts sent an unexpected answer for %s; not cached", ean)
            return None

    ttl = settings.off_found_ttl_days if product else settings.off_missing_ttl_days
    payload = {"name": product.name, "brand": product.brand, "quantity": product.quantity} if product else None
    if row is None:
        row = OffCache(ean=ean)
        db.add(row)
    row.found, row.payload, row.fetched_at, row.expires_at = product is not None, payload, now, now + timedelta(days=ttl)
    db.commit()
    return product
