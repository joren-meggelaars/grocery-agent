import pytest

from grocery.security.passwords import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    verify_password,
)


def test_hash_is_argon2id_and_verifies():
    h = hash_password("a long enough password")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "a long enough password")
    assert not verify_password(h, "a different password!!")


def test_missing_hash_never_verifies():
    assert not verify_password(None, "anything at all here")


def test_garbage_hash_never_verifies():
    assert not verify_password("not-a-hash", "whatever password")


def test_policy_rejects_short_and_huge():
    with pytest.raises(PasswordPolicyError):
        hash_password("short")
    with pytest.raises(PasswordPolicyError):
        hash_password("x" * 257)


def test_fresh_hash_does_not_need_rehash():
    assert not needs_rehash(hash_password("a long enough password"))
