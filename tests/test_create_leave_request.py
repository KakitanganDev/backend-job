"""
Tests for create_leave_request service function.

Each test covers one acceptance criterion with full isolation (in-memory SQLite).
"""

import pytest
from datetime import date, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import Employee, LeaveRequest, LeaveBalance, LeaveType, LeaveStatus
from src import services
from src.services import (
    create_leave_request,
    BackdatedRequestError,
    InvalidDateRangeError,
    OverlappingLeaveError,
    InsufficientBalanceError,
    NoWorkingDaysError,
    EmployeeNotFoundError,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

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
    # Clean up leave requests and balances after each test
    session.query(LeaveRequest).delete()
    session.query(LeaveBalance).delete()
    session.query(Employee).delete()
    session.commit()
    session.close()


@pytest.fixture
def employee(db):
    """An employee with a generous annual leave balance for 2026."""
    emp = Employee(name="Alice", email="alice@test.com", department="Engineering")
    db.add(emp)
    db.flush()
    bal = LeaveBalance(
        employee_id=emp.id,
        leave_type=LeaveType.ANNUAL,
        year=2026,
        total_days=14,
        used_days=0,
    )
    db.add(bal)
    db.commit()
    db.refresh(emp)
    return emp


@pytest.fixture(autouse=True)
def freeze_today(monkeypatch):
    """Pin _today() to 2026-05-28 so start_date=2026-06-01 is always future."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 28))


# ── Tests ─────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_creates_pending_request(self, db, employee):
        """Happy path: creates a pending request and returns it."""
        lr = create_leave_request(
            db,
            employee_id=employee.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 3),
        )
        assert lr.id is not None
        assert lr.status == LeaveStatus.PENDING
        assert lr.employee_id == employee.id
        assert lr.leave_type == LeaveType.ANNUAL
        assert lr.start_date == date(2026, 6, 1)
        assert lr.end_date == date(2026, 6, 3)

    def test_deduction_estimate_attached(self, db, employee):
        """The returned object has _estimated_deductions as a transient attribute."""
        lr = create_leave_request(
            db,
            employee_id=employee.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 3),
        )
        assert hasattr(lr, "_estimated_deductions")
        # Jun 1 (Mon), Jun 2 (Tue), Jun 3 (Wed) → 3 working days in 2026
        deductions = lr._estimated_deductions
        assert len(deductions) == 1
        assert deductions[0]["year"] == 2026
        assert deductions[0]["days"] == pytest.approx(3.0)


class TestValidationErrors:
    def test_backdated_raises(self, db, employee):
        """Start date in the past raises BackdatedRequestError."""
        with pytest.raises(BackdatedRequestError):
            create_leave_request(
                db,
                employee_id=employee.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 5),
            )

    def test_end_before_start_raises(self, db, employee):
        """end_date before start_date raises InvalidDateRangeError."""
        with pytest.raises(InvalidDateRangeError):
            create_leave_request(
                db,
                employee_id=employee.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 6, 5),
                end_date=date(2026, 6, 1),
            )

    def test_weekend_only_range_raises(self, db, employee):
        """A range that covers only weekends raises NoWorkingDaysError."""
        # 2026-06-06 (Sat) to 2026-06-07 (Sun)
        with pytest.raises(NoWorkingDaysError):
            create_leave_request(
                db,
                employee_id=employee.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 6, 6),
                end_date=date(2026, 6, 7),
            )

    def test_insufficient_balance_raises(self, db, employee):
        """Requesting more days than balance raises InsufficientBalanceError."""
        # employee has 14 days total, request 15 working days (3 weeks)
        with pytest.raises(InsufficientBalanceError):
            create_leave_request(
                db,
                employee_id=employee.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 22),  # 15 working days
            )

    def test_employee_not_found_raises(self, db):
        """Non-existent employee_id raises EmployeeNotFoundError."""
        with pytest.raises(EmployeeNotFoundError):
            create_leave_request(
                db,
                employee_id=99999,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 3),
            )


class TestOverlapDetection:
    def test_overlap_with_pending_raises(self, db, employee):
        """Overlapping with a pending request raises OverlappingLeaveError."""
        # Create first request
        create_leave_request(
            db,
            employee_id=employee.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
        )
        # Overlapping second request
        with pytest.raises(OverlappingLeaveError):
            create_leave_request(
                db,
                employee_id=employee.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 6, 3),
                end_date=date(2026, 6, 10),
            )

    def test_no_overlap_with_cancelled(self, db, employee):
        """Cancelled requests are ignored for overlap detection — should succeed."""
        # Create a cancelled leave request manually
        cancelled = LeaveRequest(
            employee_id=employee.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
            status=LeaveStatus.CANCELLED,
        )
        db.add(cancelled)
        db.commit()

        # Should succeed — no active overlap
        lr = create_leave_request(
            db,
            employee_id=employee.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
        )
        assert lr.status == LeaveStatus.PENDING

    def test_no_overlap_with_rejected(self, db, employee):
        """Rejected requests are ignored for overlap detection — should succeed."""
        rejected = LeaveRequest(
            employee_id=employee.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
            status=LeaveStatus.REJECTED,
        )
        db.add(rejected)
        db.commit()

        lr = create_leave_request(
            db,
            employee_id=employee.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
        )
        assert lr.status == LeaveStatus.PENDING


class TestHalfDay:
    def test_half_day_start_counts_half(self, db, employee):
        """half_day_start=True counts the first day as 0.5."""
        lr = create_leave_request(
            db,
            employee_id=employee.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 1),
            half_day_start=True,
        )
        deductions = lr._estimated_deductions
        assert deductions[0]["days"] == pytest.approx(0.5)

    def test_half_day_end_counts_half(self, db, employee):
        """half_day_end=True for a single day counts 0.5."""
        lr = create_leave_request(
            db,
            employee_id=employee.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 1),
            half_day_end=True,
        )
        deductions = lr._estimated_deductions
        assert deductions[0]["days"] == pytest.approx(0.5)


class TestYearSpanning:
    def test_year_spanning_request_splits_deductions(self, db):
        """A leave spanning two years has deductions split per year."""
        # Setup employee with balance for both 2026 and 2027
        emp = Employee(name="Bob", email="bob@test.com", department="Engineering")
        db.add(emp)
        db.flush()
        for yr in (2026, 2027):
            bal = LeaveBalance(
                employee_id=emp.id,
                leave_type=LeaveType.ANNUAL,
                year=yr,
                total_days=14,
                used_days=0,
            )
            db.add(bal)
        db.commit()

        # Dec 28 2026 (Mon) to Jan 5 2027 (Tue)
        lr = create_leave_request(
            db,
            employee_id=emp.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 12, 28),
            end_date=date(2027, 1, 5),
        )
        deductions = lr._estimated_deductions
        years = {d["year"]: d["days"] for d in deductions}

        # Dec 28(Mon), 29(Tue), 30(Wed), 31(Thu) → 4 days in 2026
        # Jan 1 2027 is Thursday. Jan 2 is Fri. Jan 3-4 weekend. Jan 5 Mon. → 3 days
        assert 2026 in years
        assert 2027 in years
        assert years[2026] == pytest.approx(4.0)
        assert years[2027] == pytest.approx(3.0)
