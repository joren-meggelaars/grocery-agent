"""A small Postgres-backed job queue (no Redis). Claiming uses SKIP LOCKED on Postgres."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.db.base import utcnow
from grocery.db.models import Job

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (30, 120)  # wait before attempt 2 and 3
STUCK_AFTER = timedelta(minutes=15)


def enqueue(db: Session, kind: str, payload: dict, run_after: datetime | None = None) -> Job:
    job = Job(kind=kind, payload=payload, status="queued", run_after=run_after or utcnow())
    db.add(job)
    db.flush()
    return job


def claim_next(db: Session, now: datetime | None = None) -> Job | None:
    now = now or utcnow()
    stmt = (
        select(Job)
        .where(Job.status == "queued", Job.run_after <= now)
        .order_by(Job.run_after, Job.id)
        .limit(1)
    )
    if db.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = db.scalar(stmt)
    if job is None:
        return None
    job.status = "running"
    job.attempts += 1
    job.run_after = now  # doubles as "started at" while running, for stuck-job recovery
    db.commit()
    return job


def complete(db: Session, job: Job, now: datetime | None = None) -> None:
    job.status = "done"
    job.finished_at = now or utcnow()
    job.last_error = None
    db.commit()


def fail(db: Session, job: Job, error: str, retryable: bool, now: datetime | None = None) -> bool:
    """Requeue with backoff while attempts remain; returns True if it was requeued."""
    now = now or utcnow()
    job.last_error = error[:2000]
    if retryable and job.attempts < MAX_ATTEMPTS:
        job.status = "queued"
        job.run_after = now + timedelta(seconds=BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)])
        db.commit()
        return True
    job.status = "failed"
    job.finished_at = now
    db.commit()
    return False


def recover_stuck(db: Session, now: datetime | None = None) -> int:
    """Jobs left 'running' by a crashed worker go back to the queue."""
    now = now or utcnow()
    stuck = db.scalars(
        select(Job).where(Job.status == "running", Job.run_after <= now - STUCK_AFTER)
    ).all()
    for job in stuck:
        job.status = "queued"
        job.run_after = now
    db.commit()
    return len(stuck)
