"""DB engine + session factory.

SQLite is used unconditionally — DriveForge is single-node-by-design. For
a fresh install, `init_db()` creates the schema on first boot. For
existing installs, `_auto_migrate()` adds any newly-declared columns
via ALTER TABLE (lightweight SQLite migration path; Alembic takes over
once the schema starts evolving non-trivially).
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from driveforge.db.models import Base

logger = logging.getLogger(__name__)


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False so async tasks can reuse the engine across loops
    return create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )


_SQLITE_TYPE_MAP = {
    "INTEGER": "INTEGER",
    "VARCHAR": "TEXT",
    "TEXT": "TEXT",
    "BOOLEAN": "INTEGER",  # SQLite stores bools as ints
    "DATETIME": "TEXT",
    "JSON": "TEXT",
    "FLOAT": "REAL",
}


def _auto_migrate(engine: Engine) -> None:
    """Add any missing columns declared in ORM models but absent in the DB."""
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            type_name = type(column.type).__name__.upper()
            sqlite_type = _SQLITE_TYPE_MAP.get(type_name, "TEXT")
            nullable = "" if column.nullable else " NOT NULL"
            default_clause = ""
            if column.default is not None and getattr(column.default, "is_scalar", False):
                val = column.default.arg
                if isinstance(val, bool):
                    default_clause = f" DEFAULT {1 if val else 0}"
                elif isinstance(val, (int, float)):
                    default_clause = f" DEFAULT {val}"
                elif isinstance(val, str):
                    default_clause = f" DEFAULT '{val}'"
            sql = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {sqlite_type}{nullable}{default_clause}"
            logger.info("db migration: %s", sql)
            with engine.begin() as conn:
                conn.execute(text(sql))


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _auto_migrate(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
