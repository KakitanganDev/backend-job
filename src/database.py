"""
Database engine, session factory, and a dialect-aware "serialise this
mutating transaction" helper.

The service layer uses `with_for_update()` to serialise concurrent
balance deductions. On Postgres that's a real `SELECT ... FOR UPDATE`.
On SQLite there is no row-level locking, so a mutating service path
calls `escalate_to_immediate(db)` at the start of the transaction.
That issues `BEGIN IMMEDIATE`, which acquires the SQLite database-wide
write lock — giving us the same "second writer waits" guarantee as
Postgres FOR UPDATE. Read-only paths skip the helper, so they retain
SQLite's default deferred behaviour and don't block one another.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, declarative_base, sessionmaker

load_dotenv()

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///kakitangan.db")

_is_sqlite = SQLALCHEMY_DATABASE_URL.startswith("sqlite")

_connect_args = {"check_same_thread": False} if _is_sqlite else {}

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
)


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_connect(dbapi_connection, _):
        # Disable pysqlite's auto-BEGIN handling so we can issue
        # BEGIN IMMEDIATE ourselves on the writer path.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL allows concurrent readers + one writer (vs default
        # rollback-journal mode where any writer blocks readers). Lets
        # the test fixtures and the request handler run in parallel
        # threads without spurious "database is locked" on reads.
        cursor.execute("PRAGMA journal_mode=WAL")
        # A contended writer waits for the lock instead of failing
        # immediately with SQLITE_BUSY — mirrors Postgres FOR UPDATE wait.
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _sqlite_begin_deferred(conn):
        # Default: open as DEFERRED so reads don't take a write lock.
        # The mutating paths upgrade via escalate_to_immediate().
        conn.exec_driver_sql("BEGIN")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def escalate_to_immediate(db: Session) -> None:
    """On SQLite, take the database-level write lock for this transaction.

    No-op on Postgres (real row locks via `with_for_update()` are used
    instead). Must be called at the *start* of a mutating service
    function, before any other DB work on `db` — calling it after a
    SELECT has fired the autobegin in DEFERRED mode would be too late.
    The helper handles that case by rolling back any noop-autobegun
    transaction first.
    """
    if not _is_sqlite:
        return
    # If autobegin already fired (e.g. caller passed a fresh session;
    # SQLA may still have begun a transaction on the connection), we
    # need to roll back the empty transaction and start IMMEDIATE.
    if db.in_transaction():
        db.rollback()
    conn = db.connection()  # this triggers autobegin → fires the begin event → BEGIN
    # Replace the deferred BEGIN with IMMEDIATE.
    conn.exec_driver_sql("ROLLBACK")
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
