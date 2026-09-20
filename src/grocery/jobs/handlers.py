"""Job dispatch. process_one() is what the worker loop calls; tests call it directly."""

import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from grocery.config import Settings
from grocery.db.models import Job, Receipt
from grocery.jobs import queue
from grocery.llm.client import ReaderUnavailable, ReceiptReader
from grocery.receipts.service import ExtractionRetry, run_extraction

log = logging.getLogger(__name__)

ReaderFactory = Callable[[Settings], ReceiptReader]


def _fail_receipt(db: Session, receipt_id: int, message: str) -> None:
    receipt = db.get(Receipt, receipt_id)
    if receipt is not None and receipt.status == "extracting":
        receipt.status = "failed"
        receipt.error = message
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


def process_one(
    db: Session, settings: Settings, reader_factory: ReaderFactory, now: datetime | None = None
) -> bool:
    """Run the next due job. Returns False when there was nothing to do."""
    job = queue.claim_next(db, now)
    if job is None:
        return False
    try:
        if job.kind == "extract_receipt":
            _extract_receipt(db, settings, job, reader_factory, now)
        else:
            queue.fail(db, job, f"unknown job kind {job.kind!r}", retryable=False, now=now)
    except Exception as exc:  # a bug must not kill the worker loop
        log.exception("job %s (%s) crashed", job.id, job.kind)
        db.rollback()
        queue.fail(db, job, f"{type(exc).__name__}: {exc}", retryable=False, now=now)
        if job.kind == "extract_receipt":
            _fail_receipt(db, job.payload["receipt_id"], "Something went wrong while reading this receipt.")
    return True
