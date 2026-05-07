"""
Test fixtures.

We use a file-based SQLite database (one file per test process) rather
than in-memory because the concurrency tests open two connections to
the same database from different threads. In-memory SQLite gives one
connection one database — useless for that.

The DB file path is set into DATABASE_URL *before* any `src.*` import,
so the engine in `src.database` picks it up on first import.
"""

from __future__ import annotations

import atexit
import os
import tempfile
from datetime import date

import pytest

# Override env BEFORE importing anything from src.*
# If a DATABASE_URL is already set (CI postgres job), respect it.
# Otherwise spin up a per-process SQLite file.
_db_path: str | None = None
if not os.environ.get("DATABASE_URL"):
    _db_fd, _db_path = tempfile.mkstemp(suffix=".db", prefix="kakitangan_test_")
    os.close(_db_fd)
    os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ.setdefault("OTEL_ENABLED", "false")


@atexit.register
def _cleanup_db_file():
    if _db_path is not None:
        try:
            os.unlink(_db_path)
        except OSError:
            pass


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    from src import models  # noqa: F401  -- register models on Base
    from src.database import Base, engine
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    """Fresh session per test, contents wiped between tests."""
    from src.database import SessionLocal
    from src.models import Employee, LeaveBalance, LeaveRequest

    session = SessionLocal()
    session.query(LeaveRequest).delete()
    session.query(LeaveBalance).delete()
    session.query(Employee).delete()
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seed(db):
    """Standard seed: Alice (manager), Bob & Carol (her reports)."""
    from src.models import Employee, LeaveBalance, LeaveType

    alice = Employee(name="Alice", email="alice@x.com", department="Eng")
    db.add(alice)
    db.flush()
    bob = Employee(name="Bob", email="bob@x.com", department="Eng", manager_id=alice.id)
    carol = Employee(name="Carol", email="carol@x.com", department="Eng", manager_id=alice.id)
    db.add_all([bob, carol])
    db.flush()

    year = date.today().year
    db.add_all([
        LeaveBalance(employee_id=bob.id, leave_type=LeaveType.ANNUAL, year=year, total_days=14),
        LeaveBalance(employee_id=bob.id, leave_type=LeaveType.SICK, year=year, total_days=12),
        LeaveBalance(employee_id=carol.id, leave_type=LeaveType.ANNUAL, year=year, total_days=14),
        LeaveBalance(employee_id=carol.id, leave_type=LeaveType.SICK, year=year, total_days=12),
    ])
    db.commit()
    return {"alice": alice, "bob": bob, "carol": carol, "year": year}
