"""Background worker: extraction jobs and hourly housekeeping.

    python -m grocery.jobs.worker
"""

import logging
import signal
import time

from grocery.config import Settings, get_settings
from grocery.db.base import utcnow
from grocery.db.session import make_engine, make_session_factory
from grocery.jobs import queue
from grocery.jobs.handlers import ReaderFactory, process_one
from grocery.deals.service import ensure_refresh_queued
from grocery.llm.client import make_reader
from grocery.settings_store import effective
from grocery.uploads.storage import purge_due_files

log = logging.getLogger("grocery.worker")

POLL_SECONDS = 2.0
HOUSEKEEPING_SECONDS = 3600


def housekeeping(db, settings: Settings) -> None:
    recovered = queue.recover_stuck(db)
    purged = purge_due_files(db, settings)
    if ensure_refresh_queued(db, effective(db, settings), utcnow()):
        log.info("housekeeping: queued the nightly offers refresh")
    if recovered or purged:
        log.info("housekeeping: requeued %s stuck job(s), deleted %s expired image(s)", recovered, purged)


def run(settings: Settings, reader_factory: ReaderFactory = make_reader) -> None:
    factory = make_session_factory(make_engine(settings.database_url))
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("worker started (model=%s)", settings.llm_model)
    last_housekeeping = 0.0
    while not stop:
        with factory() as db:
            if time.monotonic() - last_housekeeping >= HOUSEKEEPING_SECONDS:
                housekeeping(db, settings)
                last_housekeeping = time.monotonic()
            worked = process_one(db, settings, reader_factory, utcnow())
        if not worked:
            time.sleep(POLL_SECONDS)
    log.info("worker stopped")


if __name__ == "__main__":
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(settings)
