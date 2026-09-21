"""Shelf captures: phone upload -> worker reads the label -> confirm -> price observation."""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from grocery.config import Settings
from grocery.db.base import utcnow
from grocery.db.models import Category, Extraction, PriceObservation, ProductEan, ShelfCapture, Store
from grocery.jobs.queue import enqueue
from grocery.llm.budget import budget_state
from grocery.llm.client import ReadResult, ShelfReader
from grocery.llm.pricing import estimate_cost_eur
from grocery.llm.schemas import ShelfLabelExtraction
from grocery.money import format_cents
from grocery.prices.observations import record_shelf
from grocery.products.ean import clean_ean
from grocery.products.matching import MatchIndex
from grocery.products.off import Fetcher, OffProduct, http_fetch, lookup
from grocery.receipts.service import ConfirmError, ExtractionRetry, get_or_create_product
from grocery.uploads.images import UploadError, reencode_image, sniff_kind
from grocery.uploads.storage import read_image, remove_from_disk, retention_deadline, save_image

log = logging.getLogger(__name__)

PROMO_KINDS = ("none", "percentage", "multi_buy", "x_for_y", "fixed_price", "bonus_card", "other")


def local_date(moment: datetime, tz_name: str) -> date:
    return moment.astimezone(ZoneInfo(tz_name)).date()


# --- upload -----------------------------------------------------------------

def create_capture(
    db: Session,
    settings: Settings,
    *,
    client_uuid: str,
    ean_text: str | None,
    store_chain: str,
    captured_at: datetime | None,
    photo: bytes,
    now: datetime | None = None,
) -> tuple[ShelfCapture, bool]:
    """Returns (capture, created). Re-sending the same client_uuid returns the existing capture."""
    try:
        client_uuid = str(uuid.UUID(client_uuid))
    except (ValueError, AttributeError, TypeError) as exc:
        raise UploadError("The capture id is not valid.") from exc
    existing = db.scalar(select(ShelfCapture).where(ShelfCapture.client_uuid == client_uuid))
    if existing is not None:
        return existing, False

    ean = None
    if ean_text and ean_text.strip():
        ean = clean_ean(ean_text)
        if ean is None:
            raise UploadError("That barcode number is not valid. Scan again or check the digits.")
    store = db.scalar(select(Store).where(Store.chain == store_chain))
    if store is None:
        raise UploadError("Choose a store.")
    if not photo:
        raise UploadError("Take a photo of the price label.")
    if len(photo) > settings.max_upload_bytes:
        raise UploadError("The photo is too large.")
    if sniff_kind(photo) != "image":
        raise UploadError("The label photo must be a JPEG, PNG or WebP image.")
    image = reencode_image(photo, max_long_edge=settings.label_max_edge)

    now = now or utcnow()
    if captured_at is None or captured_at > now + timedelta(days=1):
        captured_at = now
    stored = save_image(db, settings, image, retention_deadline(settings.unconfirmed_retention_days, now))
    capture = ShelfCapture(
        client_uuid=client_uuid, ean=ean, store_id=store.id, captured_at=captured_at,
        received_at=now, file_id=stored.id, status="extracting", requires_card=False,
    )
    db.add(capture)
    try:
        db.flush()
    except IntegrityError:  # two flushes of the outbox raced: keep the first
        db.rollback()
        return db.scalar(select(ShelfCapture).where(ShelfCapture.client_uuid == client_uuid)), False
    enqueue(db, "process_capture", {"capture_id": capture.id})
    db.commit()
    return capture, True


# --- worker step ------------------------------------------------------------

def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def normalise_unit_price(cents: int | None, per: str | None) -> tuple[int | None, str | None]:
    """Everything is stored per kg or per l; per-100 prices are scaled, per piece is dropped."""
    if cents is None or per is None:
        return None, None
    if per in ("kg", "l"):
        return cents, per
    if per == "100g":
        return cents * 10, "kg"
    if per == "100ml":
        return cents * 10, "l"
    return None, None


def _record(db: Session, settings: Settings, reader: ShelfReader, capture: ShelfCapture, result: ReadResult) -> Extraction:
    u = result.usage
    extraction = Extraction(
        kind="shelf", subject_id=capture.id, model=result.model, prompt_version=reader.prompt_version,
        raw_json=result.raw, parsed_ok=result.parsed is not None, error=result.error,
        input_tokens=u.input_tokens, output_tokens=u.output_tokens,
        cache_read_tokens=u.cache_read_tokens, cache_write_tokens=u.cache_write_tokens,
        cost_est_eur=estimate_cost_eur(
            result.model, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
            settings.usd_to_eur,
        ),
        latency_ms=result.latency_ms, request_id=result.request_id,
    )
    db.add(extraction)
    db.flush()
    log.info(
        "capture %s: model=%s in=%s out=%s cost=EUR %.4f %sms ok=%s%s",
        capture.id, result.model, u.input_tokens, u.output_tokens, extraction.cost_est_eur,
        result.latency_ms, extraction.parsed_ok, f" error={result.error}" if result.error else "",
    )
    return extraction


def _fail(capture: ShelfCapture, message: str, extraction: Extraction | None = None) -> None:
    capture.status = "failed"
    capture.error = message
    if extraction is not None:
        capture.extraction_id = extraction.id


def run_capture(
    db: Session,
    settings: Settings,
    reader: ShelfReader,
    capture_id: int,
    now: datetime | None = None,
    off_fetch: Fetcher = http_fetch,
) -> None:
    capture = db.get(ShelfCapture, capture_id)
    if capture is None or capture.status != "extracting":
        return

    budget = budget_state(db, settings, now)
    if budget["exhausted"]:
        _fail(capture, f"Monthly API budget reached (EUR {budget['spent']:.2f} of {budget['cap']:.2f}).")
        db.commit()
        return
    image = read_image(settings, capture.file) if capture.file else None
    if image is None:
        _fail(capture, "The photo is no longer available. Take it again.")
        db.commit()
        return

    off: OffProduct | None = lookup(db, settings, capture.ean, off_fetch, now) if capture.ean else None

    result = reader.read([image], None)
    extraction = _record(db, settings, reader, capture, result)
    if result.parsed is None:
        if result.retryable:
            db.commit()
            raise ExtractionRetry(result.error or "temporary error")
        _fail(capture, result.error or "The label could not be read.", extraction)
        db.commit()
        return

    _apply(db, capture, result.parsed, extraction, off, settings, now, off_fetch)
    db.commit()


def _apply(
    db: Session, capture: ShelfCapture, label: ShelfLabelExtraction, extraction: Extraction,
    off: OffProduct | None, settings: Settings, now: datetime | None, off_fetch: Fetcher,
) -> None:
    if capture.ean is None:
        on_label = clean_ean(label.ean_on_label)
        if on_label:
            capture.ean = on_label
            off = lookup(db, settings, on_label, off_fetch, now)
    capture.off_name = off.display if off else None
    capture.extraction_id = extraction.id
    capture.error = None

    capture.price_cents = label.price_cents
    capture.effective_price_cents = label.effective_price_cents
    capture.unit_price_cents, capture.unit_basis = normalise_unit_price(label.unit_price_cents, label.unit_price_per)
    capture.promo_kind = label.promo_kind if label.promo_kind != "none" else None
    capture.promo_text = (label.promo_text or "")[:200] or None
    if (
        capture.promo_kind is None
        and label.regular_price_cents
        and label.price_cents
        and label.price_cents < label.regular_price_cents
    ):
        # A price below the crossed-out "van" price is a promotion, whatever the model called it.
        capture.promo_kind = "fixed_price"
        capture.promo_text = capture.promo_text or f"Was EUR {format_cents(label.regular_price_cents)}"
    capture.requires_card = label.requires_card or label.promo_kind == "bonus_card"
    capture.valid_from = _parse_date(label.valid_from)
    capture.valid_until = _parse_date(label.valid_until)

    link = db.get(ProductEan, capture.ean) if capture.ean else None
    if link is not None:
        capture.product_id = link.product_id
        capture.product_name = link.product.name
        capture.category_id = link.product.category_id
    else:
        # The name printed on the shelf label leads; Open Food Facts' name is a fallback and a second
        # chance to recognise a product the user already has.
        candidates = [n.strip() for n in (label.product_name, off.name if off else None) if n and n.strip()]
        if candidates:
            store = db.get(Store, capture.store_id)
            chain = store.chain if store else "other"
            index = MatchIndex(db)
            matches = [index.find(chain, name, name) for name in candidates]
            known = next((m for m in matches if m.source != "none"), None)
            capture.product_name = known.product_name if known else candidates[0]
            capture.category_id = known.category_id if known else None
    capture.status = "needs_review"


# --- confirm ----------------------------------------------------------------

@dataclass
class CaptureInput:
    ean_text: str
    product_name: str
    category_id: int | None
    store_chain: str
    price_cents: int | None
    effective_price_cents: int | None
    unit_price_cents: int | None
    unit_basis: str | None
    promo_kind: str | None
    promo_text: str
    requires_card: bool
    valid_from: date | None
    valid_until: date | None


def confirm_capture(
    db: Session, settings: Settings, capture: ShelfCapture, data: CaptureInput, now: datetime | None = None
) -> None:
    now = now or utcnow()
    problems = []
    ean = None
    if data.ean_text.strip():
        ean = clean_ean(data.ean_text)
        if ean is None:
            problems.append("The barcode number is not valid.")
    if not data.product_name.strip():
        problems.append("Enter the product name.")
    if not data.price_cents or data.price_cents <= 0:
        problems.append("Enter the shelf price.")
    if data.effective_price_cents is not None and data.effective_price_cents <= 0:
        problems.append("The promotion price must be more than zero.")
    if data.unit_price_cents is not None and data.unit_basis not in ("kg", "l"):
        problems.append("Choose kg or l for the price per unit.")
    if data.promo_kind and data.promo_kind not in PROMO_KINDS:
        problems.append("Unknown promotion type.")
    store = db.scalar(select(Store).where(Store.chain == data.store_chain))
    if store is None:
        problems.append("Choose a store.")
    if problems:
        raise ConfirmError(problems)

    product = get_or_create_product(db, data.product_name, data.category_id)
    if ean:
        link = db.get(ProductEan, ean)
        if link is None:
            db.add(ProductEan(ean=ean, product_id=product.id, source="scan"))
        elif link.product_id != product.id:
            link.product_id = product.id  # the user's answer replaces the old link

    capture.ean = ean
    capture.store_id = store.id
    capture.product_id = product.id
    capture.product_name = product.name
    capture.category_id = product.category_id
    capture.price_cents = data.price_cents
    capture.effective_price_cents = data.effective_price_cents
    capture.unit_price_cents = data.unit_price_cents
    capture.unit_basis = data.unit_basis if data.unit_price_cents is not None else None
    capture.promo_kind = data.promo_kind or None
    capture.promo_text = data.promo_text.strip()[:200] or None
    capture.requires_card = data.requires_card
    capture.valid_from = data.valid_from
    capture.valid_until = data.valid_until
    capture.status = "confirmed"
    capture.confirmed_at = now
    capture.error = None
    db.flush()

    record_shelf(db, capture, local_date(capture.captured_at, settings.timezone))
    if capture.file is not None and capture.file.deleted_at is None:
        capture.file.delete_after = now + timedelta(days=settings.confirmed_retention_days)
    db.commit()


def retry_capture(db: Session, capture: ShelfCapture) -> None:
    if capture.status != "failed":
        return
    capture.status = "extracting"
    capture.error = None
    enqueue(db, "process_capture", {"capture_id": capture.id})
    db.commit()


def delete_capture(db: Session, settings: Settings, capture: ShelfCapture) -> None:
    file = capture.file
    if file is not None:
        remove_from_disk(settings, file)
    db.execute(
        sql_delete(PriceObservation).where(
            PriceObservation.source == "shelf", PriceObservation.source_ref_id == capture.id
        )
    )
    db.delete(capture)
    db.flush()
    if file is not None:
        db.delete(file)
    db.commit()


def review_count(db: Session) -> int:
    return db.scalar(select(func.count()).where(ShelfCapture.status == "needs_review")) or 0


def categories(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).order_by(Category.sort_order)))
