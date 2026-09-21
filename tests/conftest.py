import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from grocery.cli import create_user
from grocery.config import Settings
from grocery.db.base import Base
from grocery.main import create_app
from grocery.refdata import seed

PASSWORD = "correct horse battery staple"
PEER = ("172.30.90.1", 50000)  # the docker bridge gateway: what Tailscale Serve looks like

# Set TEST_DATABASE_URL (e.g. postgresql+psycopg://postgres@127.0.0.1:5432/grocery_test) to run the whole suite on
# PostgreSQL instead of SQLite. Every test gets its own schema, dropped afterwards, so tests stay isolated.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def pg_url():
    if not TEST_DATABASE_URL:
        yield None
        return
    schema = "t_" + uuid.uuid4().hex[:12]
    admin = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    joiner = "&" if "?" in TEST_DATABASE_URL else "?"
    yield f"{TEST_DATABASE_URL}{joiner}options=-csearch_path%3D{schema}"
    with admin.connect() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


@pytest.fixture
def make_settings(tmp_path, pg_url):
    def _make(**overrides) -> Settings:
        values = dict(
            database_url=pg_url or f"sqlite:///{tmp_path / 'test.db'}",
            files_dir=tmp_path / "files",
            allowed_hosts=["testserver"],
            cookie_secure=True,
            ts_identity_mode="off",
            ts_trusted_proxy_ips=["172.30.90.1/32"],
            ts_allowed_logins=["joren@example.com"],
        )
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return _make


@pytest.fixture
def make_app(make_settings):
    def _make(**overrides):
        app = create_app(make_settings(**overrides))
        Base.metadata.create_all(app.state.engine)
        with app.state.session_factory() as db:
            seed(db)
            create_user(db, "joren", PASSWORD, tailscale_login="joren@example.com")
        return app

    return _make


@pytest.fixture
def make_client(make_app):
    def _make(app=None, headers=None, peer=PEER, **overrides) -> TestClient:
        app = app or make_app(**overrides)
        return TestClient(
            app,
            base_url="https://testserver",
            client=peer,
            follow_redirects=False,
            headers={"Origin": "https://testserver", **(headers or {})},
        )

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def db(client):
    with client.app.state.session_factory() as session:
        yield session


def login(client: TestClient, password: str = PASSWORD, username: str = "joren", next_url: str = "/"):
    return client.post(
        "/login", data={"username": username, "password": password, "next": next_url}
    )


def csrf_of(client: TestClient) -> str:
    """Scrape the session's CSRF token from a rendered page, as a browser form would."""
    html = client.get("/").text
    marker = 'name="csrf_token" value="'
    return html.split(marker, 1)[1].split('"', 1)[0]
