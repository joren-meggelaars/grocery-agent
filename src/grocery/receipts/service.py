"""Receipt lifecycle: upload -> extraction job -> review -> confirm (or quick manual add)."""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.config import Settings
from grocery.db.base import utcnow
from grocery.db.models import (
    Category,
    Extraction,
    NameMapping,
    Product,
    Receipt,
    ReceiptFile,
    ReceiptLine,
    Store,
)
from grocery.jobs.queue import enqueue
from grocery.llm.budget import budget_state
from grocery.llm.client import ReadResult, ReceiptReader
from grocery.llm.pricing import estimate_cost_eur
from grocery.llm.schemas import ReceiptExtraction
from grocery.products.matching import Match, MatchIndex
from grocery.products.normalize import normalize_raw, product_key
from grocery.receipts.validation import LineData, ValidationResult, expected_line_total, validate_receipt
from grocery.refdata import FALLBACK_CATEGORY, QUICKADD_CATEGORY, QUICKADD_NAME
from grocery.uploads.images import UploadError, reencode_image, sniff_kind
from grocery.uploads.pdf import rasterize_pdf
from grocery.uploads.storage import (
    is_duplicate_hash,
    read_image,
    remove_from_disk,
    retention_deadline,
    save_image,
    schedule_deletion,
)

log = logging.getLogger(__name__)


class ExtractionRetry(Exception):
    """Raised inside the job handler to make the queue retry with backoff."""


class ConfirmError(ValueError):
    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


# --- upload -----------------------------------------------------------------

def create_from_upload(
    db: Session, settings: Settings, files: list[bytes], now: datetime | None = None
) -> Receipt:
    if not files:
        raise UploadError("Choose at least one photo or a PDF.")
    if len(files) > settings.max_photos:
        raise UploadError(f"Upload at most {settings.max_photos} files at a time.")
    kinds = []
    for data in files:
        if not data:
            raise UploadError("One of the files was empty.")
        if len(data) > settings.max_upload_bytes:
            raise UploadError(f"A file is larger than {settings.max_upload_bytes // (1024 * 1024)} MB.")
        kind = sniff_kind(data)
        if kind is None:
            raise UploadError("Unsupported file type. Use a photo (JPEG, PNG, WebP) or a PDF.")
        kinds.append(kind)
    if "pdf" in kinds and len(files) > 1:
        raise UploadError("Upload either a single PDF or photos, not a mix.")

    source_text = None
    if kinds[0] == "pdf":
        content = rasterize_pdf(files[0])
        images = [reencode_image(page) for page in content.pages]
        source = "pdf"
        if content.has_text_layer:
            source_text = content.text[:20000]
    else:
        images = [reencode_image(data) for data in files]
        source = "photo"

    receipt = Receipt(status="extracting", source=source, source_text=source_text, flags=[])
    db.add(receipt)
    db.flush()
    deadline = retention_deadline(settings.unconfirmed_retention_days, now)
    duplicate = False
    for page_no, image in enumerate(images, start=1):
        duplicate = duplicate or is_duplicate_hash(db, image.sha256)
        stored = save_image(db, settings, image, deadline)
        db.add(ReceiptFile(receipt_id=receipt.id, file_id=stored.id, page_no=page_no))
    if duplicate:
        receipt.flags = ["duplicate_photo"]
    enqueue(db, "extract_receipt", {"receipt_id": receipt.id})
    db.commit()
    return receipt


# --- extraction -------------------------------------------------------------

def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _fail(receipt: Receipt, message: str, extraction: Extraction | None = None) -> None:
    receipt.status = "failed"
    receipt.error = message
    if extraction is not None:
        receipt.extraction_id = extraction.id


def _record_extraction(db: Session, settings: Settings, reader: ReceiptReader, receipt: Receipt, result: ReadResult) -> Extraction:
    u = result.usage
    extraction = Extraction(
        kind="receipt",
        subject_id=receipt.id,
        model=result.model,
        prompt_version=reader.prompt_version,
        raw_json=result.raw,
        parsed_ok=result.parsed is not None,
        error=result.error,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_tokens=u.cache_read_tokens,
        cache_write_tokens=u.cache_write_tokens,
        cost_est_eur=estimate_cost_eur(
            result.model,
            u.input_tokens,
            u.output_tokens,
            u.cache_read_tokens,
            u.cache_write_tokens,
            settings.usd_to_eur,
        ),
        latency_ms=result.latency_ms,
        request_id=result.request_id,
    )
    db.add(extraction)
    db.flush()
    log.info(
        "receipt %s: model=%s in=%s out=%s cache_read=%s cost=EUR %.4f %sms ok=%s%s",
        receipt.id, result.model, u.input_tokens, u.output_tokens, u.cache_read_tokens,
        extraction.cost_est_eur, result.latency_ms, extraction.parsed_ok,
        f" error={result.error}" if result.error else "",
    )
    return extraction


def run_extraction(
    db: Session, settings: Settings, reader: ReceiptReader, receipt_id: int, now: datetime | None = None
) -> None:
    receipt = db.get(Receipt, receipt_id)
    if receipt is None or receipt.status != "extracting":
        return

    budget = budget_state(db, settings, now)
    if budget["exhausted"]:
        _fail(
            receipt,
            f"Monthly API budget reached (EUR {budget['spent']:.2f} of {budget['cap']:.2f}). "
            "Raise LLM_MONTHLY_BUDGET_EUR to continue, then retry.",
        )
        db.commit()
        return

    if receipt.source_text:
        images, text = [], receipt.source_text
    else:
        images = [img for rf in receipt.files if (img := read_image(settings, rf.file)) is not None]
        text = None
        if not images:
            _fail(receipt, "The photos are no longer available. Upload the receipt again.")
            db.commit()
            return

    result = reader.read(images, text)
    extraction = _record_extraction(db, settings, reader, receipt, result)

    if result.parsed is None:
        if result.retryable:
            db.commit()
            raise ExtractionRetry(result.error or "temporary error")
        _fail(receipt, result.error or "The receipt could not be read.", extraction)
        db.commit()
        return

    _apply_extraction(db, receipt, result.parsed, extraction)
    db.commit()


def _apply_extraction(db: Session, receipt: Receipt, parsed: ReceiptExtraction, extraction: Extraction) -> None:
    store = db.scalar(select(Store).where(Store.chain == parsed.store_chain))
    chain = store.chain if store else "other"
    receipt.store_id = store.id if store else None
    receipt.purchased_on = _parse_date(parsed.purchase_date)
    receipt.total_cents = parsed.total_cents
    receipt.extraction_id = extraction.id
    receipt.error = None

    index = MatchIndex(db)
    categories = {c.name: c.id for c in db.scalars(select(Category))}
    receipt.lines.clear()
    for number, line in enumerate(parsed.lines, start=1):
        match = (
            index.find(chain, line.raw_text, line.suggested_name)
            if line.kind == "item"
            else Match("none", None, None, None, None)
        )
        receipt.lines.append(
            ReceiptLine(
                line_no=number,
                kind=line.kind,
                raw_text=line.raw_text[:255],
                quantity_milli=max(0, round(line.quantity * 1000)),
                unit=line.unit,
                unit_price_cents=line.unit_price_cents,
                line_total_cents=line.line_total_cents,
                product_id=match.product_id,
                suggested_name=(line.suggested_name or "")[:200] or None,
                # a learned product's category beats the model's guess
                category_id=match.category_id or categories.get(line.category or ""),
                match_source=match.source,
                match_confidence=match.confidence,
            )
        )
    db.flush()
    refresh_flags(db, receipt)
    receipt.status = "needs_review"


def line_data(lines) -> list[LineData]:
    return [
        LineData(l.line_no, l.kind, l.quantity_milli, l.unit_price_cents, l.line_total_cents) for l in lines
    ]


def refresh_flags(db: Session, receipt: Receipt) -> ValidationResult:
    result = validate_receipt(line_data(receipt.lines), receipt.total_cents, receipt.purchased_on)
    receipt.sum_delta_cents = result.delta_cents
    flags = list(result.flags)
    if "duplicate_photo" in (receipt.flags or []):
        flags.append("duplicate_photo")
    if is_possible_duplicate(db, receipt):
        flags.append("possible_duplicate")
    receipt.flags = flags
    return result


def is_possible_duplicate(db: Session, receipt: Receipt) -> bool:
    if receipt.store_id is None or receipt.purchased_on is None or receipt.total_cents is None:
        return False
    return (
        db.scalar(
            select(Receipt.id)
            .where(
                Receipt.id != receipt.id,
                Receipt.status == "confirmed",
                Receipt.store_id == receipt.store_id,
                Receipt.purchased_on == receipt.purchased_on,
                Receipt.total_cents == receipt.total_cents,
            )
            .limit(1)
        )
        is not None
    )


def retry_receipt(db: Session, receipt: Receipt) -> None:
    if receipt.status != "failed":
        return
    receipt.status = "extracting"
    receipt.error = None
    enqueue(db, "extract_receipt", {"receipt_id": receipt.id})
    db.commit()


# --- confirm ----------------------------------------------------------------

@dataclass
class LineInput:
    orig_no: int | None
    kind: str
    raw_text: str
    quantity_milli: int
    unit: str
    unit_price_cents: int | None
    line_total_cents: int
    product_name: str
    category_id: int | None


@dataclass
class ConfirmInput:
    store_chain: str
    purchased_on: date | None
    total_cents: int | None
    lines: list[LineInput] = field(default_factory=list)
    accept_mismatch: bool = False


def _get_or_create_product(db: Session, name: str, category_id: int | None) -> Product:
    name = " ".join(name.split())[:200]
    key = product_key(name)
    product = db.scalar(select(Product).where(Product.name_key == key))
    if product is None:
        product = Product(name=name, name_key=key, category_id=category_id)
        db.add(product)
        db.flush()
    elif category_id and product.category_id != category_id:
        product.category_id = category_id  # the user's correction wins
    return product


def _learn_mapping(db: Session, chain: str, raw_text: str, product: Product, now: datetime) -> None:
    key = normalize_raw(raw_text)
    if not key:
        return
    mapping = db.scalar(select(NameMapping).where(NameMapping.chain == chain, NameMapping.raw_norm == key))
    if mapping is None:
        db.add(NameMapping(chain=chain, raw_norm=key, product_id=product.id, last_confirmed_at=now))
    elif mapping.product_id == product.id:
        mapping.confirmed_count += 1
        mapping.last_confirmed_at = now
    else:  # the user re-pointed this text: the new answer replaces the old one
        mapping.product_id = product.id
        mapping.confirmed_count = 1
        mapping.last_confirmed_at = now


def confirm_receipt(
    db: Session, settings: Settings, receipt: Receipt, data: ConfirmInput, now: datetime | None = None
) -> ValidationResult:
    now = now or utcnow()
    problems = []
    if data.purchased_on is None:
        problems.append("Enter the purchase date.")
    if data.total_cents is None:
        problems.append("Enter the receipt total.")
    if not data.lines:
        problems.append("A receipt needs at least one line.")
    if problems:
        raise ConfirmError(problems)

    result = validate_receipt(
        [
            LineData(i, l.kind, l.quantity_milli, l.unit_price_cents, l.line_total_cents)
            for i, l in enumerate(data.lines, start=1)
        ],
        data.total_cents,
        data.purchased_on,
    )
    if result.delta_cents != 0 and not data.accept_mismatch:
        raise ConfirmError(
            [f"The lines are EUR {abs(result.delta_cents) / 100:.2f} "
             f"{'below' if result.delta_cents > 0 else 'above'} the total. Fix a line or tick 'save anyway'."]
        )

    store = db.scalar(select(Store).where(Store.chain == data.store_chain)) or db.scalar(
        select(Store).where(Store.chain == "other")
    )
    chain = store.chain
    originals = {
        l.line_no: (l.raw_text, l.quantity_milli, l.unit_price_cents, l.line_total_cents, l.kind,
                    l.product_id, l.match_source, l.match_confidence)
        for l in receipt.lines
    }

    new_lines = []
    for number, li in enumerate(data.lines, start=1):
        product = None
        if li.kind == "item" and li.product_name.strip():
            product = _get_or_create_product(db, li.product_name, li.category_id)
            _learn_mapping(db, chain, li.raw_text, product, now)
        orig = originals.get(li.orig_no) if li.orig_no else None
        edited = orig is None or orig[:5] != (li.raw_text[:255], li.quantity_milli, li.unit_price_cents,
                                               li.line_total_cents, li.kind)
        if product is None:
            source, confidence = "none", None
        elif orig is not None and orig[5] == product.id:
            source, confidence = orig[6], orig[7]
        else:
            source, confidence = "manual", None
        new_lines.append(
            ReceiptLine(
                line_no=number,
                kind=li.kind,
                raw_text=li.raw_text[:255],
                quantity_milli=li.quantity_milli,
                unit=li.unit,
                unit_price_cents=li.unit_price_cents,
                line_total_cents=li.line_total_cents,
                product_id=product.id if product else None,
                suggested_name=None,
                category_id=li.category_id,
                match_source=source,
                match_confidence=confidence,
                edited=edited,
            )
        )

    receipt.lines.clear()
    db.flush()
    receipt.lines.extend(new_lines)
    receipt.store_id = store.id
    receipt.purchased_on = data.purchased_on
    receipt.total_cents = data.total_cents
    receipt.status = "confirmed"
    receipt.confirmed_at = now
    receipt.error = None
    db.flush()
    refresh_flags(db, receipt)
    schedule_deletion(db, receipt.id, now + timedelta(days=settings.confirmed_retention_days))
    db.commit()
    return result


# --- manual quick-add (bakery, Turkish supermarket, anything without a receipt photo) -------------

def quick_add(
    db: Session,
    *,
    store_chain: str,
    purchased_on: date | None,
    description: str,
    total_cents: int | None,
    weight_milli: int | None = None,
    price_per_kg_cents: int | None = None,
    now: datetime | None = None,
) -> Receipt:
    now = now or utcnow()
    store = db.scalar(select(Store).where(Store.chain == store_chain))
    if store is None:
        raise ConfirmError(["Choose a store."])
    if purchased_on is None:
        raise ConfirmError(["Enter the date."])
    if total_cents is None and weight_milli and price_per_kg_cents:
        total_cents = expected_line_total(weight_milli, price_per_kg_cents)
    if total_cents is None or total_cents <= 0:
        raise ConfirmError(["Enter the amount, or a weight and a price per kg."])

    name = " ".join(description.split())[:200] or QUICKADD_NAME.get(store_chain, "Groceries")
    category = db.scalar(
        select(Category).where(Category.name == QUICKADD_CATEGORY.get(store_chain, FALLBACK_CATEGORY))
    )
    product = _get_or_create_product(db, name, category.id if category else None)
    weighed = bool(weight_milli and price_per_kg_cents)

    receipt = Receipt(
        store_id=store.id,
        purchased_on=purchased_on,
        total_cents=total_cents,
        status="confirmed",
        source="manual",
        sum_delta_cents=0,
        flags=[],
        confirmed_at=now,
    )
    receipt.lines.append(
        ReceiptLine(
            line_no=1,
            kind="item",
            raw_text=name,
            quantity_milli=weight_milli if weighed else 1000,
            unit="kg" if weighed else "pcs",
            unit_price_cents=price_per_kg_cents if weighed else None,
            line_total_cents=total_cents,
            product_id=product.id,
            category_id=product.category_id,
            match_source="manual",
            edited=True,
        )
    )
    db.add(receipt)
    db.commit()
    return receipt


# --- delete -----------------------------------------------------------------

def delete_receipt(db: Session, settings: Settings, receipt: Receipt) -> None:
    files = [rf.file for rf in receipt.files]
    for file in files:
        remove_from_disk(settings, file)
    db.delete(receipt)  # cascades to receipt_files and lines
    db.flush()
    for file in files:
        db.delete(file)
    db.commit()
