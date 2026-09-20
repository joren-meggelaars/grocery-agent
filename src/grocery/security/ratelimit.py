"""Login throttling.

Per username: after FREE_FAILURES consecutive failures the account is locked for
60 s, doubling per further failure up to 15 min. Attempts made *during* a lockout
are logged but do not extend it, so an attacker cannot keep the owner locked out
indefinitely. Unknown usernames are counted exactly like real ones, so the
response never reveals which usernames exist.

Global: more than GLOBAL_MAX failures in GLOBAL_WINDOW seconds blocks all login
attempts until the window clears. Per-IP limiting is deliberately absent: behind
Tailscale Serve every request arrives from the proxy address.
"""

import math
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grocery.db.base import utcnow
from grocery.db.models import AuthEvent

FREE_FAILURES = 5
BASE_DELAY = 60
MAX_DELAY = 900
GLOBAL_WINDOW = 600
GLOBAL_MAX = 30

_RESET_KINDS = ("login_ok", "unlock")


def record(
    db: Session,
    kind: str,
    username: str,
    ip: str | None,
    detail: str | None = None,
    now: datetime | None = None,
) -> None:
    db.add(
        AuthEvent(
            ts=now or utcnow(),
            username=username[:64],
            ip=(ip or "")[:64] or None,
            kind=kind,
            detail=(detail or "")[:255] or None,
        )
    )
    db.commit()


def consecutive_failures(db: Session, username: str) -> tuple[int, datetime | None]:
    reset_ts = db.scalar(
        select(func.max(AuthEvent.ts)).where(
            AuthEvent.username == username, AuthEvent.kind.in_(_RESET_KINDS)
        )
    )
    q = select(func.count(), func.max(AuthEvent.ts)).where(
        AuthEvent.username == username, AuthEvent.kind == "login_fail"
    )
    if reset_ts is not None:
        q = q.where(AuthEvent.ts > reset_ts)
    count, last = db.execute(q).one()
    return count, last


def lockout_remaining(db: Session, username: str, now: datetime | None = None) -> int:
    """Seconds until another attempt is allowed (0 = allowed now)."""
    now = now or utcnow()
    count, last = consecutive_failures(db, username)
    if count < FREE_FAILURES or last is None:
        return 0
    delay = min(BASE_DELAY * 2 ** (count - FREE_FAILURES), MAX_DELAY)
    remaining = delay - (now - last).total_seconds()
    return max(0, math.ceil(remaining))


def globally_limited(db: Session, now: datetime | None = None) -> bool:
    now = now or utcnow()
    since = now - timedelta(seconds=GLOBAL_WINDOW)
    count = db.scalar(
        select(func.count()).where(AuthEvent.kind == "login_fail", AuthEvent.ts > since)
    )
    return (count or 0) >= GLOBAL_MAX
