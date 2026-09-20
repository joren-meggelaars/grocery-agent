"""User management. Run inside the container:

    docker compose run --rm app python -m grocery.cli create-user joren
"""

import argparse
import getpass
import re
import sys
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.config import get_settings
from grocery.db.models import User
from grocery.db.session import make_engine, make_session_factory
from grocery.security import ratelimit
from grocery.security.passwords import PasswordPolicyError, hash_password
from grocery.security.sessions import revoke_all

_USERNAME = re.compile(r"^[a-z0-9._-]{1,64}$")


class CliError(Exception):
    pass


def _normalise(username: str) -> str:
    name = username.strip().lower()
    if not _USERNAME.match(name):
        raise CliError("Username must be 1-64 characters of a-z 0-9 . _ -")
    return name


def _get_user(db: Session, username: str) -> User:
    user = db.scalar(select(User).where(User.username == _normalise(username)))
    if user is None:
        raise CliError(f"No such user: {username}")
    return user


def create_user(db: Session, username: str, password: str, tailscale_login: str | None = None) -> User:
    name = _normalise(username)
    if db.scalar(select(User).where(User.username == name)):
        raise CliError(f"User already exists: {name}")
    try:
        password_hash = hash_password(password)
    except PasswordPolicyError as exc:
        raise CliError(str(exc)) from exc
    user = User(
        username=name,
        password_hash=password_hash,
        tailscale_login=tailscale_login.strip().lower() if tailscale_login else None,
    )
    db.add(user)
    db.commit()
    return user


def set_password(db: Session, username: str, password: str) -> None:
    user = _get_user(db, username)
    try:
        user.password_hash = hash_password(password)
    except PasswordPolicyError as exc:
        raise CliError(str(exc)) from exc
    revoke_all(db, user.id)  # a password change signs out every device
    db.commit()


def unlock(db: Session, username: str) -> None:
    user = _get_user(db, username)
    ratelimit.record(db, "unlock", user.username, None, "cli")


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise CliError("Passwords do not match.")
    return first


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grocery.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="create the login user")
    p.add_argument("username")
    p.add_argument("--tailscale-login", help="Tailscale account e-mail (for TS_IDENTITY_MODE=sso)")
    p.add_argument("--password-stdin", action="store_true")

    p = sub.add_parser("set-password", help="change a password (signs out all devices)")
    p.add_argument("username")
    p.add_argument("--password-stdin", action="store_true")

    p = sub.add_parser("unlock", help="clear a login lockout")
    p.add_argument("username")

    p = sub.add_parser("revoke-sessions", help="sign a user out everywhere")
    p.add_argument("username")
    return parser


def main(argv: Sequence[str] | None = None, session_factory: Callable[[], Session] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if session_factory is None:
        session_factory = make_session_factory(make_engine(get_settings().database_url))
    try:
        with session_factory() as db:
            if args.command == "create-user":
                user = create_user(
                    db, args.username, _read_password(args.password_stdin), args.tailscale_login
                )
                print(f"Created user {user.username}.")
            elif args.command == "set-password":
                set_password(db, args.username, _read_password(args.password_stdin))
                print("Password changed; all sessions revoked.")
            elif args.command == "unlock":
                unlock(db, args.username)
                print("Lockout cleared.")
            elif args.command == "revoke-sessions":
                count = revoke_all(db, _get_user(db, args.username).id)
                print(f"Revoked {count} session(s).")
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
