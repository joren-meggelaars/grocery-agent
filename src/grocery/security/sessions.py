import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from grocery.config import Settings
from grocery.db.base import utcnow
from grocery.db.models import AuthSession, User

# last_seen is only rewritten when it is older than this, to avoid a write per request.
_TOUCH_INTERVAL = timedelta(minutes=5)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(
    db: Session,
    user: User,
    settings: Settings,
    user_agent: str | None,
    now: datetime | None = None,
) -> tuple[str, AuthSession]:
    """Returns (cookie token, row). Only the token's hash is stored."""
    now = now or utcnow()
    token = secrets.token_urlsafe(32)
    row = AuthSession(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_secret=secrets.token_urlsafe(32),
        created_at=now,
        last_seen=now,
        expires_at=now + timedelta(days=settings.session_absolute_days),
        user_agent=(user_agent or "")[:255] or None,
    )
    db.add(row)
    db.commit()
    return token, row


def lookup_session(
    db: Session, token: str | None, settings: Settings, now: datetime | None = None
) -> AuthSession | None:
    if not token:
        return None
    now = now or utcnow()
    row = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    if row is None:
        return None
    idle_limit = row.last_seen + timedelta(days=settings.session_idle_days)
    if now >= row.expires_at or now >= idle_limit or not row.user.is_active:
        db.delete(row)
        db.commit()
        return None
    if now - row.last_seen > _TOUCH_INTERVAL:
        row.last_seen = now
        db.commit()
    return row


def revoke_session(db: Session, session_id: int, user_id: int) -> bool:
    result = db.execute(
        delete(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == user_id)
    )
    db.commit()
    return result.rowcount > 0


def revoke_all(db: Session, user_id: int, except_id: int | None = None) -> int:
    stmt = delete(AuthSession).where(AuthSession.user_id == user_id)
    if except_id is not None:
        stmt = stmt.where(AuthSession.id != except_id)
    result = db.execute(stmt)
    db.commit()
    return result.rowcount
