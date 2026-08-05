"""Explicit PostgreSQL engine and Session factory construction."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

INVALID_DATABASE_URL = "database URL must use postgresql+psycopg"


def create_postgres_engine(database_url: str) -> Engine:
    """Create the only supported SQLAlchemy engine without hidden global state."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql" or url.get_driver_name() != "psycopg":
        raise ValueError(INVALID_DATABASE_URL)
    return create_engine(url, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Bind short-lived transactional Sessions to an explicit engine."""
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
