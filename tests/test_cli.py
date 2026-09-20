import io
import sys

from sqlalchemy import select

from grocery.cli import main
from grocery.db.models import AuthSession, User
from grocery.security import ratelimit
from grocery.security.passwords import verify_password
from tests.conftest import PASSWORD, login


def _stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def test_create_user_from_stdin(client, db, monkeypatch, capsys):
    _stdin(monkeypatch, "another long password\n")
    code = main(["create-user", "Alice", "--password-stdin"], client.app.state.session_factory)
    assert code == 0
    user = db.scalar(select(User).where(User.username == "alice"))
    assert verify_password(user.password_hash, "another long password")
    assert "Created user alice" in capsys.readouterr().out


def test_create_user_rejects_a_short_password(client, monkeypatch, capsys):
    _stdin(monkeypatch, "short\n")
    assert main(["create-user", "bob", "--password-stdin"], client.app.state.session_factory) == 1
    assert "at least 12" in capsys.readouterr().err


def test_create_user_rejects_duplicates_and_bad_names(client, monkeypatch, capsys):
    factory = client.app.state.session_factory
    _stdin(monkeypatch, "another long password\n")
    assert main(["create-user", "joren", "--password-stdin"], factory) == 1
    assert "already exists" in capsys.readouterr().err
    _stdin(monkeypatch, "another long password\n")
    assert main(["create-user", "bad name!", "--password-stdin"], factory) == 1


def test_set_password_signs_out_every_device(client, db, monkeypatch):
    login(client)
    assert db.scalar(select(AuthSession)) is not None
    _stdin(monkeypatch, "a brand new password\n")
    assert main(["set-password", "joren", "--password-stdin"], client.app.state.session_factory) == 0
    db.expire_all()
    assert db.scalar(select(AuthSession)) is None
    assert login(client, password="a brand new password").status_code == 303


def test_unlock_clears_a_lockout(client, db):
    for _ in range(ratelimit.FREE_FAILURES):
        login(client, password="wrong wrong wrong")
    assert login(client).status_code == 429
    assert main(["unlock", "joren"], client.app.state.session_factory) == 0
    assert login(client, password=PASSWORD).status_code == 303


def test_revoke_sessions(client, db, capsys):
    login(client)
    assert main(["revoke-sessions", "joren"], client.app.state.session_factory) == 0
    assert "Revoked 1" in capsys.readouterr().out


def test_unknown_user_is_an_error_not_a_traceback(client, capsys):
    assert main(["unlock", "ghost"], client.app.state.session_factory) == 1
    assert "No such user" in capsys.readouterr().err
