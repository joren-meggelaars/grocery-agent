"""Transient image storage: random filenames on a private volume, never served statically."""

import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.config import Settings
from grocery.db.base import utcnow
from grocery.db.models import ReceiptFile, StoredFile
from grocery.uploads.images import ProcessedImage

log = logging.getLogger(__name__)


def _resolve(settings: Settings, rel: str) -> Path:
    """Absolute path for a stored relative path; refuses anything outside files_dir."""
    root = settings.files_dir.resolve()
    path = (root / rel).resolve()
    if root != path and root not in path.parents:
        raise ValueError(f"path escapes the files directory: {rel!r}")
    return path


def save_image(
    db: Session, settings: Settings, image: ProcessedImage, delete_after: datetime | None
) -> StoredFile:
    token = secrets.token_hex(16)
    rel = f"{token[:2]}/{token}.jpg"
    path = _resolve(settings, rel)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "xb") as fh:
        fh.write(image.data)
    row = StoredFile(
        sha256=image.sha256,
        mime="image/jpeg",
        bytes=len(image.data),
        width=image.width,
        height=image.height,
        path=rel,
        delete_after=delete_after,
    )
    db.add(row)
    db.flush()
    return row


def read_image(settings: Settings, file: StoredFile) -> bytes | None:
    if file.deleted_at is not None or not file.path:
        return None
    try:
        return _resolve(settings, file.path).read_bytes()
    except FileNotFoundError:
        return None


def remove_from_disk(settings: Settings, file: StoredFile) -> None:
    if file.path:
        try:
            _resolve(settings, file.path).unlink(missing_ok=True)
        except (OSError, ValueError):
            log.exception("could not delete %s", file.path)


def is_duplicate_hash(db: Session, sha256: str, exclude_ids: set[int] | None = None) -> bool:
    rows = db.scalars(select(StoredFile.id).where(StoredFile.sha256 == sha256)).all()
    return any(r not in (exclude_ids or set()) for r in rows)


def schedule_deletion(db: Session, receipt_id: int, when: datetime) -> None:
    for rf in db.scalars(select(ReceiptFile).where(ReceiptFile.receipt_id == receipt_id)):
        if rf.file.deleted_at is None:
            rf.file.delete_after = when


def purge_due_files(db: Session, settings: Settings, now: datetime | None = None) -> int:
    """Delete files past their delete_after from disk; keep a tombstone row. Returns the count."""
    now = now or utcnow()
    due = db.scalars(
        select(StoredFile).where(
            StoredFile.delete_after.is_not(None),
            StoredFile.delete_after <= now,
            StoredFile.deleted_at.is_(None),
        )
    ).all()
    for f in due:
        if f.path:
            try:
                _resolve(settings, f.path).unlink(missing_ok=True)
            except (OSError, ValueError):
                log.exception("could not delete %s", f.path)
                continue
        f.path = None
        f.deleted_at = now
    db.commit()
    return len(due)


def retention_deadline(days: int, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(days=days)
