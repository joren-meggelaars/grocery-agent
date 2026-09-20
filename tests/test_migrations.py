from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from grocery.db import models  # noqa: F401
from grocery.db.base import Base

ROOT = Path(__file__).resolve().parent.parent


def _config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    return cfg


def test_migrations_match_the_models_and_are_reversible(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = _config(url)

    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"models and migrations have drifted: {diff}"

    command.downgrade(cfg, "base")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert tables <= {"alembic_version"}


def test_migration_seeds_match_the_reference_data(tmp_path):
    from sqlalchemy import text

    from grocery import refdata

    url = f"sqlite:///{tmp_path / 's.db'}"
    command.upgrade(_config(url), "head")
    with create_engine(url).connect() as conn:
        stores = conn.execute(text("select chain, name, role from stores order by id")).all()
        cats = conn.execute(text("select name, counts_as_food from categories order by sort_order")).all()
        price = conn.execute(text("select value from settings where key = :k"), {"k": refdata.TURKISH_PRICE_KEY}).scalar()
    assert [tuple(r) for r in stores] == refdata.STORES
    assert [(n, bool(f)) for n, f in cats] == refdata.CATEGORIES
    assert price == "849"
