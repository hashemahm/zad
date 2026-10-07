from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = make_url(get_settings().database_url)
    kwargs = {}
    if url.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)

    if url.get_backend_name() == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """create_all() does not alter existing tables, so add columns introduced since."""
    columns = {c["name"] for c in inspect(engine).get_columns("content_items")}
    if "channel_id" not in columns:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE content_items ADD COLUMN channel_id INTEGER "
                "REFERENCES channels(id) ON DELETE SET NULL"
            ))
            conn.execute(text("CREATE INDEX ix_content_items_channel_id ON content_items (channel_id)"))


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
