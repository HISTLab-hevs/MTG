"""Database engine, session factory and the request-scoped session dependency."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base every model in app.models inherits from."""


def _pool_kwargs_for(database_url: str) -> dict:
    """Connection-pool settings for the configured database.

    Endpoints are sync, so each runs in an anyio worker thread and holds its
    connection for the whole request, chat turns included, which block on a model
    call for seconds. The pool is therefore sized to the anyio threadpool rather
    than to the average load: on SQLAlchemy's defaults the pool runs out while
    the database sits idle. pre_ping turns a connection gone stale, after a
    restart or an idled-out socket, into a transparent reconnect.

    Args:
        database_url: The configured URL.

    Returns:
        The keyword arguments for create_engine; empty for SQLite, which the
        tests use and which needs none.
    """
    if database_url.startswith("sqlite"):
        return {}
    return {
        "pool_size": 20,
        "max_overflow": 30,
        "pool_timeout": 30,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }


_settings = get_settings()
_is_sqlite = _settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

engine = create_engine(
    _settings.database_url,
    connect_args=_connect_args,
    **_pool_kwargs_for(_settings.database_url),
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_enforce_foreign_keys(dbapi_connection, _record):
        """Turn on foreign keys, which SQLite leaves off per connection.

        The schema leans on ON DELETE CASCADE, so without this the tests would
        not exercise the deletion behaviour Postgres enforces in production.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """Yield a session for one request, closed when the request ends."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
