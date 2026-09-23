"""Job dispatch. process_one() is what the worker loop calls; tests call it directly."""

import logging
import time
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.capture.service import local_date, run_capture
from grocery.config import Settings
from grocery.db.base import utcnow
from grocery.db.models import CupboardScan, DealRun, Job, Receipt, ShelfCapture
from grocery.deals import service as deals
from grocery.deals.client import Fetcher as DealsFetcher
from grocery.deals.client import PrijsProfeet, http_get
from grocery.settings_store import effective
from grocery.jobs import queue
from grocery.llm.client import ImageReader, ReaderUnavailable, make_shelf_reader
from grocery.products.off import Fetcher, http_fetch, lookup
from grocery.receipts.service import ExtractionRetry, run_extraction

log = logging.getLogger(__name__)

ReaderFactory = Callable[[Settings], ImageReader]


def _fail_receipt(db: Session, receipt_id: int, message: str) -> None:
    receipt = db.get(Receipt, receipt_id)
    if receipt is not None and receipt.status == "extracting":
        receipt.status = "failed"
        receipt.error = message
        db.commit()


def _fail_capture(db: Session, capture_id: int, message: str) -> None:
    capture = db.get(ShelfCapture, capture_id)
    if capture is not None and capture.status == "extracting":
        capture.status = "failed"
        capture.error = message
        db.commit()


def _extract_receipt(db: Session, settings: Settings, job: Job, reader_factory: ReaderFactory, now) -> None:
    receipt_id = job.payload["receipt_id"]
    try:
        reader = reader_factory(settings)
    except ReaderUnavailable as exc:
        _fail_receipt(db, receipt_id, f"{exc}. Add it to .env on the server and retry.")
        queue.complete(db, job, now)
        return
    try:
        run_extraction(db, settings, reader, receipt_id, now)
    except ExtractionRetry as exc:
        if not queue.fail(db, job, str(exc), retryable=True, now=now):
            _fail_receipt(db, receipt_id, f"The receipt could not be read after several attempts: {exc}")
        return
    queue.complete(db, job, now)


def _process_capture(
    db: Session, settings: Settings, job: Job, reader_factory: ReaderFactory, off_fetch: Fetcher, now
) -> None:
    capture_id = job.payload["capture_id"]
    try:
        reader = reader_factory(settings)
    except ReaderUnavailable as exc:
        _fail_capture(db, capture_id, f"{exc}. Add it to .env on the server and retry.")
        queue.complete(db, job, now)
        return
    try:
        run_capture(db, settings, reader, capture_id, now, off_fetch)
    except ExtractionRetry as exc:
        if not queue.fail(db, job, str(exc), retryable=True, now=now):
            _fail_capture(db, capture_id, f"The label could not be read after several attempts: {exc}")
        return
    queue.complete(db, job, now)


def _lookup_ean(db: Session, settings: Settings, job: Job, off_fetch: Fetcher, now) -> None:
    """Fill in what Open Food Facts calls a scanned barcode. Unknown or unreachable is fine: the user names it."""
    ean = job.payload["ean"]
    pending = db.scalar(select(CupboardScan).where(CupboardScan.ean == ean))
    if pending is not None:
        product = lookup(db, settings, ean, off_fetch, now)
        pending.off_name = product.display if product else None
        pending.status = "needs_name"
        db.commit()
    queue.complete(db, job, now)


def _deals_refresh(
    db: Session, settings: Settings, job: Job, deals_fetch: DealsFetcher, ha_post: deals.HaPost, sleep, now
) -> None:
    """One chunk of the nightly offers refresh; a run that is not finished queues its own next chunk."""
    now = now or utcnow()
    eff = effective(db, settings)
    if eff.deals_enabled != "on":
        queue.complete(db, job, now)
        return
    today = local_date(now, eff.timezone)
    run = db.get(DealRun, job.payload.get("run_id")) if job.payload.get("run_id") else None
    if run is None or run.status != "running":
        run = deals.start_run(db, eff, today, now)
    key = settings.prijsprofeet_api_key.get_secret_value() if settings.prijsprofeet_api_key else None
    source = PrijsProfeet(settings.deals_user_agent, key, fetch=deals_fetch, sleep=sleep)
    if deals.run_chunk(db, run, source, eff, now) or not run.plan:
        deals.finish_run(db, run, settings, eff, today, now, ha_post)
    else:
        queue.enqueue(db, deals.JOB_KIND, {"run_id": run.id}, now)
    queue.complete(db, job, now)


def process_one(
    db: Session,
    settings: Settings,
    reader_factory: ReaderFactory,
    now: datetime | None = None,
    *,
    shelf_reader_factory: ReaderFactory = make_shelf_reader,
    off_fetch: Fetcher = http_fetch,
    deals_fetch: DealsFetcher = http_get,
    ha_post: deals.HaPost = deals.ha_post,
    sleep=time.sleep,
) -> bool:
    """Run the next due job. Returns False when there was nothing to do."""
    job = queue.claim_next(db, now)
    if job is None:
        return False
    try:
        if job.kind == "extract_receipt":
            _extract_receipt(db, settings, job, reader_factory, now)
        elif job.kind == "process_capture":
            _process_capture(db, settings, job, shelf_reader_factory, off_fetch, now)
        elif job.kind == deals.JOB_KIND:
            _deals_refresh(db, settings, job, deals_fetch, ha_post, sleep, now)
        elif job.kind == "lookup_ean":
            _lookup_ean(db, settings, job, off_fetch, now)
        else:
            queue.fail(db, job, f"unknown job kind {job.kind!r}", retryable=False, now=now)
    except Exception as exc:  # a bug must not kill the worker loop
        log.exception("job %s (%s) crashed", job.id, job.kind)
        db.rollback()
        queue.fail(db, job, f"{type(exc).__name__}: {exc}", retryable=False, now=now)
        if job.kind == "extract_receipt":
            _fail_receipt(db, job.payload["receipt_id"], "Something went wrong while reading this receipt.")
        elif job.kind == "process_capture":
            _fail_capture(db, job.payload["capture_id"], "Something went wrong while reading this label.")
        elif job.kind == "lookup_ean":  # never leave a barcode stuck in "lookup": the user can still name it
            pending = db.scalar(select(CupboardScan).where(CupboardScan.ean == job.payload["ean"]))
            if pending is not None:
                pending.status = "needs_name"
                db.commit()
    return True
