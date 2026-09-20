from datetime import timedelta

from grocery.db.base import utcnow
from grocery.security import ratelimit


def _fail(db, username, at, n=1):
    for _ in range(n):
        ratelimit.record(db, "login_fail", username, "1.2.3.4", now=at)


def test_free_failures_do_not_lock(db):
    now = utcnow()
    _fail(db, "joren", now, ratelimit.FREE_FAILURES - 1)
    assert ratelimit.lockout_remaining(db, "joren", now) == 0


def test_lock_starts_at_threshold_and_expires(db):
    now = utcnow()
    _fail(db, "joren", now, ratelimit.FREE_FAILURES)
    assert ratelimit.lockout_remaining(db, "joren", now) == ratelimit.BASE_DELAY
    later = now + timedelta(seconds=ratelimit.BASE_DELAY + 1)
    assert ratelimit.lockout_remaining(db, "joren", later) == 0


def test_delay_doubles_and_is_capped(db):
    now = utcnow()
    _fail(db, "joren", now, ratelimit.FREE_FAILURES + 1)
    assert ratelimit.lockout_remaining(db, "joren", now) == ratelimit.BASE_DELAY * 2
    _fail(db, "joren", now, 20)
    assert ratelimit.lockout_remaining(db, "joren", now) == ratelimit.MAX_DELAY


def test_attempts_during_lockout_do_not_extend_it(db):
    now = utcnow()
    _fail(db, "joren", now, ratelimit.FREE_FAILURES)
    for _ in range(10):
        ratelimit.record(db, "lockout", "joren", "1.2.3.4", now=now + timedelta(seconds=5))
    assert ratelimit.lockout_remaining(db, "joren", now) == ratelimit.BASE_DELAY


def test_success_resets_the_counter(db):
    now = utcnow()
    _fail(db, "joren", now, ratelimit.FREE_FAILURES)
    ratelimit.record(db, "login_ok", "joren", None, now=now + timedelta(seconds=1))
    assert ratelimit.consecutive_failures(db, "joren")[0] == 0


def test_unlock_resets_the_counter(db):
    now = utcnow()
    _fail(db, "joren", now, ratelimit.FREE_FAILURES)
    ratelimit.record(db, "unlock", "joren", None, now=now + timedelta(seconds=1))
    assert ratelimit.lockout_remaining(db, "joren", now + timedelta(seconds=2)) == 0


def test_unknown_usernames_are_throttled_like_real_ones(db):
    now = utcnow()
    _fail(db, "no-such-user", now, ratelimit.FREE_FAILURES)
    assert ratelimit.lockout_remaining(db, "no-such-user", now) == ratelimit.BASE_DELAY


def test_users_are_throttled_independently(db):
    now = utcnow()
    _fail(db, "mallory", now, ratelimit.FREE_FAILURES)
    assert ratelimit.lockout_remaining(db, "joren", now) == 0


def test_global_limit_counts_only_recent_failures(db):
    now = utcnow()
    _fail(db, "a", now - timedelta(seconds=ratelimit.GLOBAL_WINDOW + 60), ratelimit.GLOBAL_MAX)
    assert not ratelimit.globally_limited(db, now)
    _fail(db, "b", now, ratelimit.GLOBAL_MAX)
    assert ratelimit.globally_limited(db, now)
