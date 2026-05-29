"""
Tests for cancel_leave_request service function.
"""

import pytest
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import Employee, LeaveRequest, LeaveBalance, LeaveType, LeaveStatus


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture
def db(engine):
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    yield session
    session.rollback()
    # Clean up all data between tests
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.commit()
    session.close()


@pytest.fixture
def setup_employee(db):
    """Create an employee with annual leave balance."""
    emp = Employee(name="Test Employee", email="test@company.com", department="Engineering")
    db.add(emp)
    db.flush()

    year = date.today().year
    balance = LeaveBalance(
        employee_id=emp.id,
        leave_type=LeaveType.ANNUAL,
        year=year,
        total_days=14,
        used_days=0,
    )
    db.add(balance)
    db.commit()
    db.refresh(emp)
    return emp


@pytest.fixture
def setup_other_employee(db):
    """Create a second employee (not the owner)."""
    emp = Employee(name="Other Employee", email="other@company.com", department="HR")
    db.add(emp)
    db.commit()
    db.refresh(emp)
    return emp


def make_pending_leave(db, employee_id, start_offset=7, days=3):
    """Helper: create a PENDING leave request."""
    today = date.today()
    lr = LeaveRequest(
        employee_id=employee_id,
        leave_type=LeaveType.ANNUAL,
        start_date=today + timedelta(days=start_offset),
        end_date=today + timedelta(days=start_offset + days - 1),
        status=LeaveStatus.PENDING,
    )
    db.add(lr)
    db.commit()
    db.refresh(lr)
    return lr


def make_approved_leave(db, employee_id, start_offset=7, days=3):
    """Helper: create an APPROVED leave request with a deduction row."""
    from src.models import LeaveDeduction
    today = date.today()
    year = today.year
    lr = LeaveRequest(
        employee_id=employee_id,
        leave_type=LeaveType.ANNUAL,
        start_date=today + timedelta(days=start_offset),
        end_date=today + timedelta(days=start_offset + days - 1),
        status=LeaveStatus.APPROVED,
    )
    db.add(lr)
    db.flush()

    # Set used_days on balance
    balance = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == employee_id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == year,
    ).first()
    if balance:
        balance.used_days = float(days)

    # Create deduction row
    deduction = LeaveDeduction(
        leave_request_id=lr.id,
        year=year,
        days=float(days),
    )
    db.add(deduction)
    db.commit()
    db.refresh(lr)
    return lr


# ────────────────────────────────────────────────────────────────────────────
# Test 1: Cancel pending request → status=CANCELLED, balance unchanged, _restored_deductions=[]
# ────────────────────────────────────────────────────────────────────────────

def test_cancel_pending_request(db, setup_employee):
    from src.services import cancel_leave_request

    emp = setup_employee
    lr = make_pending_leave(db, emp.id)

    result = cancel_leave_request(db, lr.id, emp.id)

    assert result.status == LeaveStatus.CANCELLED
    assert result._restored_deductions == []

    # Balance should be unchanged (was never deducted)
    year = date.today().year
    balance = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == emp.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == year,
    ).first()
    assert balance.used_days == 0


# ────────────────────────────────────────────────────────────────────────────
# Test 2: Cancel approved request → status=CANCELLED, balance restored, deduction rows deleted
# ────────────────────────────────────────────────────────────────────────────

def test_cancel_approved_request_restores_balance(db, setup_employee):
    from src.services import cancel_leave_request
    from src.models import LeaveDeduction

    emp = setup_employee
    days = 3
    lr = make_approved_leave(db, emp.id, days=days)

    year = date.today().year
    balance_before = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == emp.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == year,
    ).first()
    assert balance_before.used_days == float(days)

    result = cancel_leave_request(db, lr.id, emp.id)

    assert result.status == LeaveStatus.CANCELLED
    assert len(result._restored_deductions) == 1
    assert result._restored_deductions[0]["days"] == float(days)
    assert result._restored_deductions[0]["year"] == year

    # Balance should be restored
    db.refresh(balance_before)
    assert balance_before.used_days == 0.0

    # Deduction rows should be deleted
    remaining = db.query(LeaveDeduction).filter(
        LeaveDeduction.leave_request_id == lr.id
    ).count()
    assert remaining == 0


# ────────────────────────────────────────────────────────────────────────────
# Test 3: Cancel year-spanning approved request → both years restored
# ────────────────────────────────────────────────────────────────────────────

def test_cancel_year_spanning_approved_request(db, setup_employee):
    from src.services import cancel_leave_request
    from src.models import LeaveDeduction

    emp = setup_employee
    today = date.today()
    year = today.year
    next_year = year + 1

    # Create balance for next year
    balance_next = LeaveBalance(
        employee_id=emp.id,
        leave_type=LeaveType.ANNUAL,
        year=next_year,
        total_days=14,
        used_days=0,
    )
    db.add(balance_next)
    db.flush()

    # Create approved leave request spanning years
    lr = LeaveRequest(
        employee_id=emp.id,
        leave_type=LeaveType.ANNUAL,
        start_date=date(year, 12, 29),
        end_date=date(next_year, 1, 2),
        status=LeaveStatus.APPROVED,
    )
    db.add(lr)
    db.flush()

    # Set used days on balances
    balance_this = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == emp.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == year,
    ).first()
    balance_this.used_days = 3.0
    balance_next.used_days = 2.0

    # Create two deduction rows (one per year)
    ded1 = LeaveDeduction(leave_request_id=lr.id, year=year, days=3.0)
    ded2 = LeaveDeduction(leave_request_id=lr.id, year=next_year, days=2.0)
    db.add_all([ded1, ded2])
    db.commit()

    result = cancel_leave_request(db, lr.id, emp.id)

    assert result.status == LeaveStatus.CANCELLED
    assert len(result._restored_deductions) == 2

    db.refresh(balance_this)
    db.refresh(balance_next)
    assert balance_this.used_days == 0.0
    assert balance_next.used_days == 0.0

    remaining = db.query(LeaveDeduction).filter(
        LeaveDeduction.leave_request_id == lr.id
    ).count()
    assert remaining == 0


# ────────────────────────────────────────────────────────────────────────────
# Test 4: Non-owner cancel → NotRequestOwnerError
# ────────────────────────────────────────────────────────────────────────────

def test_non_owner_cancel_raises_error(db, setup_employee, setup_other_employee):
    from src.services import cancel_leave_request, NotRequestOwnerError

    owner = setup_employee
    other = setup_other_employee
    lr = make_pending_leave(db, owner.id)

    with pytest.raises(NotRequestOwnerError, match="only the request owner can cancel this leave"):
        cancel_leave_request(db, lr.id, other.id)


# ────────────────────────────────────────────────────────────────────────────
# Test 5: Cancel already-cancelled → AlreadyCancelledError
# ────────────────────────────────────────────────────────────────────────────

def test_cancel_already_cancelled_raises_error(db, setup_employee):
    from src.services import cancel_leave_request, AlreadyCancelledError

    emp = setup_employee
    lr = LeaveRequest(
        employee_id=emp.id,
        leave_type=LeaveType.ANNUAL,
        start_date=date.today() + timedelta(days=7),
        end_date=date.today() + timedelta(days=9),
        status=LeaveStatus.CANCELLED,
    )
    db.add(lr)
    db.commit()

    with pytest.raises(AlreadyCancelledError, match="this request is already cancelled"):
        cancel_leave_request(db, lr.id, emp.id)


# ────────────────────────────────────────────────────────────────────────────
# Test 6: Cancel rejected → RejectedRequestNotCancellableError
# ────────────────────────────────────────────────────────────────────────────

def test_cancel_rejected_raises_error(db, setup_employee):
    from src.services import cancel_leave_request, RejectedRequestNotCancellableError

    emp = setup_employee
    lr = LeaveRequest(
        employee_id=emp.id,
        leave_type=LeaveType.ANNUAL,
        start_date=date.today() + timedelta(days=7),
        end_date=date.today() + timedelta(days=9),
        status=LeaveStatus.REJECTED,
    )
    db.add(lr)
    db.commit()

    with pytest.raises(RejectedRequestNotCancellableError, match="rejected requests cannot be cancelled"):
        cancel_leave_request(db, lr.id, emp.id)


# ────────────────────────────────────────────────────────────────────────────
# Test 7: Second cancel after first succeeds → AlreadyCancelledError (deduction rows gone)
# ────────────────────────────────────────────────────────────────────────────

def test_second_cancel_after_first_raises_error(db, setup_employee):
    from src.services import cancel_leave_request, AlreadyCancelledError

    emp = setup_employee
    lr = make_approved_leave(db, emp.id, days=3)

    # First cancel succeeds
    result = cancel_leave_request(db, lr.id, emp.id)
    assert result.status == LeaveStatus.CANCELLED

    # Second cancel should raise AlreadyCancelledError
    with pytest.raises(AlreadyCancelledError, match="this request is already cancelled"):
        cancel_leave_request(db, lr.id, emp.id)


# ────────────────────────────────────────────────────────────────────────────
# Test 8: Not found → LeaveRequestNotFoundError
# ────────────────────────────────────────────────────────────────────────────

def test_cancel_not_found_raises_error(db, setup_employee):
    from src.services import cancel_leave_request, LeaveRequestNotFoundError

    emp = setup_employee
    with pytest.raises(LeaveRequestNotFoundError, match="leave request 99999 not found"):
        cancel_leave_request(db, 99999, emp.id)
