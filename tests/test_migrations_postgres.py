"""Migrations on a real PostgreSQL (skipped unless TEST_DATABASE_URL is set).

SQLite cannot show what production will do: JSONB columns, enforced string lengths, real constraint names.
"""

import os
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from grocery.db import models  # noqa: F401
from grocery.db.base import Base
from tests.test_migrations import _config

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set TEST_DATABASE_URL to run against PostgreSQL")


@pytest.fixture
def schema_url():
    schema = "m_" + uuid.uuid4().hex[:12]
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    yield f"{URL}{'&' if '?' in URL else '?'}options=-csearch_path%3D{schema}", schema
    with admin.connect() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def test_migrations_apply_match_the_models_and_reverse_on_postgres(schema_url):
    url, schema = schema_url
    cfg = _config(url)

    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
        assert diff == [], f"models and migrations have drifted on PostgreSQL: {diff}"
        columns = {c["name"]: str(c["type"]) for c in inspect(conn).get_columns("extractions")}
    assert columns["raw_json"] == "JSONB"  # the JSON variant really became JSONB

    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")  # and up again
    engine.dispose()


def test_seed_data_is_present_after_migrating_on_postgres(schema_url):
    url, _ = schema_url
    command.upgrade(_config(url), "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from stores")).scalar() == 8
        assert conn.execute(text("select count(*) from categories")).scalar() == 10
        assert conn.execute(text("select value from settings where key = 'quickadd.turkish.price_per_kg_cents'")).scalar() == "849"
    engine.dispose()
