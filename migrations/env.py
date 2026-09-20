from alembic import context
from sqlalchemy import create_engine, pool

from grocery.config import get_settings
from grocery.db import models  # noqa: F401  (registers tables on Base.metadata)
from grocery.db.base import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # config.attributes lets tests inject a URL without configparser "%" escaping issues.
    return config.attributes.get("url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
