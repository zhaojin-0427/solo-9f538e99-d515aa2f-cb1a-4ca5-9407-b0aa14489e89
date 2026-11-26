"""SQLite engine / session setup.

Two session flavours are exposed:

* ``SessionLocal`` - deferred transaction, used by read endpoints.  The first
  SELECT pins a read snapshot on the WAL database, so every query in the same
  request sees exactly the shift data committed when that first read happened.
* ``WriteSessionLocal`` - ``BEGIN IMMEDIATE``, used by every endpoint that
  mutates data.  Writers take the database write lock up front (waiting on
  ``busy_timeout`` instead of failing mid-transaction) and read the latest
  committed shift once the lock is held.

WAL lets readers and writers overlap: a slow ``/holds`` query never blocks an
ingredient registration or a packing submission, and its result never includes
half of a concurrent submission.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_PATH = os.environ.get("DB_PATH", "/data/kitchen.db")

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={
        "check_same_thread": False,
        "timeout": 30,
        # SQLAlchemy emits BEGIN itself (see the "begin" listener below), so the
        # sqlite3 driver must not start its own implicit transactions.
        "isolation_level": None,
    },
    future=True,
)

# Used by write endpoints: the first statement opens a write transaction.
write_engine = engine.execution_options(sqlite_begin="immediate")

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
WriteSessionLocal = sessionmaker(bind=write_engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        # Wait for the writer lock rather than failing immediately when another
        # request is committing.
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _enable_wal() -> None:
    """Switch the database file to WAL once, at startup.

    The journal mode is persistent in the database file, so this only matters
    for a volume created by an older version.  Readers keep their snapshot on a
    WAL database while writers commit, which is what makes the read endpoints
    snapshot reads and keeps them from blocking submissions.  Doing this here
    (not in the per-connection listener) avoids every connection racing for the
    exclusive lock needed to change the journal mode.

    A PRAGMA issued through SQLAlchemy would run inside the BEGIN that the
    dialect emits, and SQLite refuses to change the journal mode inside a
    transaction - hence the raw DBAPI connection.
    """
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        finally:
            cursor.close()
    finally:
        raw.close()
    if str(mode).lower() != "wal":
        raise RuntimeError(f"could not enable WAL journal mode (got {mode!r})")


@event.listens_for(engine, "begin")
def _begin(connection) -> None:
    mode = connection.get_execution_options().get("sqlite_begin", "deferred")
    if mode == "immediate":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        connection.exec_driver_sql("BEGIN")


class Base(DeclarativeBase):
    pass


def get_db():
    """Read-only session: consistent snapshot, no write-back."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_write_db():
    """Write session: BEGIN IMMEDIATE, so mutations are serialized."""
    db = WriteSessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    # Import models so that they register on Base before create_all.
    from app import models  # noqa: F401

    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _enable_wal()
