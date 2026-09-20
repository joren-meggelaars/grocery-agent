from datetime import timedelta

from sqlalchemy import select

from grocery.db.base import utcnow
from grocery.db.models import AuthSession, User
from grocery.security.sessions import (
    create_session,
    hash_token,
    lookup_session,
    revoke_all,
    revoke_session,
)


def _user(db):
    return db.scalar(select(User))


def test_only_the_token_hash_is_stored(client, db):
    token, row = create_session(db, _user(db), client.app.state.settings, "iPhone")
    assert row.token_hash == hash_token(token)
    assert token not in (row.token_hash, row.csrf_secret)


def test_lookup_returns_the_session(client, db):
    settings = client.app.state.settings
    token, row = create_session(db, _user(db), settings, "iPhone")
    assert lookup_session(db, token, settings).id == row.id


def test_unknown_or_empty_token_is_rejected(client, db):
    settings = client.app.state.settings
    assert lookup_session(db, "nope", settings) is None
    assert lookup_session(db, None, settings) is None


def test_idle_expiry(client, db):
    settings = client.app.state.settings
    token, _ = create_session(db, _user(db), settings, None)
    later = utcnow() + timedelta(days=settings.session_idle_days + 1)
    assert lookup_session(db, token, settings, now=later) is None
    assert db.scalar(select(AuthSession)) is None  # expired rows are deleted


def test_absolute_expiry_even_if_active(client, db):
    settings = client.app.state.settings
    start = utcnow()
    token, _ = create_session(db, _user(db), settings, None, now=start)
    # Touched every 10 days, so never idle, but past the absolute limit.
    for day in range(10, settings.session_absolute_days, 10):
        assert lookup_session(db, token, settings, now=start + timedelta(days=day))
    end = start + timedelta(days=settings.session_absolute_days + 1)
    assert lookup_session(db, token, settings, now=end) is None


def test_inactive_user_session_is_rejected(client, db):
    settings = client.app.state.settings
    user = _user(db)
    token, _ = create_session(db, user, settings, None)
    user.is_active = False
    db.commit()
    assert lookup_session(db, token, settings) is None


def test_revoke_is_scoped_to_the_owner(client, db):
    settings = client.app.state.settings
    user = _user(db)
    _, row = create_session(db, user, settings, None)
    assert not revoke_session(db, row.id, user_id=user.id + 999)
    assert revoke_session(db, row.id, user_id=user.id)


def test_revoke_all_keeps_the_excepted_session(client, db):
    settings = client.app.state.settings
    user = _user(db)
    _, keep = create_session(db, user, settings, None)
    create_session(db, user, settings, None)
    create_session(db, user, settings, None)
    assert revoke_all(db, user.id, except_id=keep.id) == 2
    assert [s.id for s in db.scalars(select(AuthSession))] == [keep.id]
