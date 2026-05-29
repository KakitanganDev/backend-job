"""Tests verifying seed_demo_data produces the expected baseline data."""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import Employee, LeaveBalance, LeaveType, Holiday
from src.services import seed_demo_data


@pytest.fixture(scope="module")
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    seed_demo_data(session)
    yield session
    session.close()


def test_four_employees_exist(db):
    count = db.query(Employee).count()
    assert count == 4


def test_at_least_two_holidays_exist(db):
    count = db.query(Holiday).count()
    assert count >= 2


def test_david_has_annual_balance(db):
    david = db.query(Employee).filter(Employee.email == "david@company.com").first()
    assert david is not None
    year = date.today().year
    balance = (
        db.query(LeaveBalance)
        .filter(
            LeaveBalance.employee_id == david.id,
            LeaveBalance.leave_type == LeaveType.ANNUAL,
            LeaveBalance.year == year,
        )
        .first()
    )
    assert balance is not None


def test_alice_has_annual_balance(db):
    alice = db.query(Employee).filter(Employee.email == "alice@company.com").first()
    assert alice is not None
    year = date.today().year
    balance = (
        db.query(LeaveBalance)
        .filter(
            LeaveBalance.employee_id == alice.id,
            LeaveBalance.leave_type == LeaveType.ANNUAL,
            LeaveBalance.year == year,
        )
        .first()
    )
    assert balance is not None
