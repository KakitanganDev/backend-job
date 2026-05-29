"""
Tests for approve_leave_request service function.

Covers:
- Happy path approval (deduction recorded, balance decremented)
- Rejection (no balance change)
- Self-approval blocked
- Non-manager blocked
- Double-approval idempotency (second call raises RequestNotPendingError, balance unchanged)
- Balance re-check: balance drops between submission and approval → InsufficientBalanceError
- Year-spanning approval → two LeaveDeduction rows, two balance updates
"""

import pytest
from datetime import date, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import Employee, LeaveRequest, LeaveBalance, LeaveDeduction, LeaveType, LeaveStatus
from src import services
from src.services import (
    approve_leave_request,
    SelfApprovalError,
    RequestNotPendingError,
    NotAuthorizedApproverError,
    InsufficientBalanceError,
    LeaveRequestNotFoundError,
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
    session.query(LeaveDeduction).delete()
    session.query(LeaveRequest).delete()
    session.query(LeaveBalance).delete()
    session.query(Employee).delete()
    session.commit()
    session.close()


def _make_employee(db, name, email, manager=None):
    emp = Employee(name=name, email=email, department="Engineering", manager=manager)
    db.add(emp)
    db.flush()
    return emp


def _make_balance(db, employee_id, leave_type, year, total_days, used_days=0):
    bal = LeaveBalance(
        employee_id=employee_id,
        leave_type=leave_type,
        year=year,
        total_days=total_days,
        used_days=used_days,
    )
    db.add(bal)
    db.flush()
    return bal


def _make_pending_request(db, employee_id, leave_type, start_date, end_date,
                           half_day_start=False, half_day_end=False):
    lr = LeaveRequest(
        employee_id=employee_id,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        status=LeaveStatus.PENDING,
        half_day_start=half_day_start,
        half_day_end=half_day_end,
    )
    db.add(lr)
    db.commit()
    db.refresh(lr)
    return lr


# ── Happy path approval ───────────────────────────────────────────────────

class TestHappyPathApproval:
    def test_approval_sets_status_approved(self, db):
        """Approving a pending request sets status to APPROVED."""
        manager = _make_employee(db, "Alice Manager", "alice@test.com")
        employee = _make_employee(db, "Bob Employee", "bob@test.com", manager=manager)
        _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)
        )

        result = approve_leave_request(
            db, lr.id, approver_id=manager.id, decision=LeaveStatus.APPROVED
        )

        assert result.status == LeaveStatus.APPROVED
        assert result.approved_by == manager.id
        assert result.approved_at is not None

    def test_approval_inserts_leave_deduction(self, db):
        """Approving a 3-day request inserts one LeaveDeduction row."""
        manager = _make_employee(db, "Alice Manager", "alice2@test.com")
        employee = _make_employee(db, "Bob Employee", "bob2@test.com", manager=manager)
        _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)  # Mon–Wed = 3 working days
        )

        approve_leave_request(
            db, lr.id, approver_id=manager.id, decision=LeaveStatus.APPROVED
        )

        deductions = db.query(LeaveDeduction).filter(
            LeaveDeduction.leave_request_id == lr.id
        ).all()
        assert len(deductions) == 1
        assert deductions[0].year == 2026
        assert deductions[0].days == pytest.approx(3.0)

    def test_approval_increments_used_days(self, db):
        """Approving a 3-day request increases used_days by 3.0."""
        manager = _make_employee(db, "Alice Manager", "alice3@test.com")
        employee = _make_employee(db, "Bob Employee", "bob3@test.com", manager=manager)
        bal = _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)
        bal_id = bal.id

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)
        )

        approve_leave_request(
            db, lr.id, approver_id=manager.id, decision=LeaveStatus.APPROVED
        )

        db.expire_all()
        updated_bal = db.query(LeaveBalance).filter(LeaveBalance.id == bal_id).first()
        assert updated_bal.used_days == pytest.approx(3.0)


# ── Rejection ─────────────────────────────────────────────────────────────

class TestRejection:
    def test_rejection_sets_status_rejected(self, db):
        """Rejecting a pending request sets status to REJECTED."""
        manager = _make_employee(db, "Alice Manager", "alice_rej@test.com")
        employee = _make_employee(db, "Bob Employee", "bob_rej@test.com", manager=manager)
        _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)
        )

        result = approve_leave_request(
            db, lr.id, approver_id=manager.id, decision=LeaveStatus.REJECTED
        )

        assert result.status == LeaveStatus.REJECTED
        assert result.approved_by == manager.id

    def test_rejection_does_not_change_balance(self, db):
        """Rejection must not change leave balance."""
        manager = _make_employee(db, "Alice Manager", "alice_rej2@test.com")
        employee = _make_employee(db, "Bob Employee", "bob_rej2@test.com", manager=manager)
        bal = _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)
        bal_id = bal.id

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)
        )

        approve_leave_request(
            db, lr.id, approver_id=manager.id, decision=LeaveStatus.REJECTED
        )

        db.expire_all()
        updated_bal = db.query(LeaveBalance).filter(LeaveBalance.id == bal_id).first()
        assert updated_bal.used_days == pytest.approx(0.0)

    def test_rejection_inserts_no_deduction(self, db):
        """Rejection must not insert any LeaveDeduction rows."""
        manager = _make_employee(db, "Alice Manager", "alice_rej3@test.com")
        employee = _make_employee(db, "Bob Employee", "bob_rej3@test.com", manager=manager)
        _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)
        )

        approve_leave_request(
            db, lr.id, approver_id=manager.id, decision=LeaveStatus.REJECTED
        )

        deductions = db.query(LeaveDeduction).filter(
            LeaveDeduction.leave_request_id == lr.id
        ).all()
        assert len(deductions) == 0


# ── Self-approval ─────────────────────────────────────────────────────────

class TestSelfApproval:
    def test_self_approval_raises(self, db):
        """Employee cannot approve their own leave request."""
        manager = _make_employee(db, "Alice Manager", "alice_self@test.com")
        employee = _make_employee(db, "Bob Employee", "bob_self@test.com", manager=manager)
        _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)
        )

        with pytest.raises(SelfApprovalError):
            approve_leave_request(
                db, lr.id, approver_id=employee.id, decision=LeaveStatus.APPROVED
            )


# ── Non-manager ───────────────────────────────────────────────────────────

class TestNonManagerApproval:
    def test_non_manager_raises(self, db):
        """Non-manager cannot approve a leave request."""
        manager = _make_employee(db, "Alice Manager", "alice_nonmgr@test.com")
        employee = _make_employee(db, "Bob Employee", "bob_nonmgr@test.com", manager=manager)
        stranger = _make_employee(db, "Carol Stranger", "carol_nonmgr@test.com")
        _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)
        )

        with pytest.raises(NotAuthorizedApproverError):
            approve_leave_request(
                db, lr.id, approver_id=stranger.id, decision=LeaveStatus.APPROVED
            )


# ── Double-approval idempotency ───────────────────────────────────────────

class TestDoubleApproval:
    def test_second_approval_raises_request_not_pending(self, db):
        """Second approval of same request raises RequestNotPendingError."""
        manager = _make_employee(db, "Alice Manager", "alice_dbl@test.com")
        employee = _make_employee(db, "Bob Employee", "bob_dbl@test.com", manager=manager)
        _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)
        )

        # First approval succeeds
        approve_leave_request(
            db, lr.id, approver_id=manager.id, decision=LeaveStatus.APPROVED
        )

        # Second approval raises RequestNotPendingError
        with pytest.raises(RequestNotPendingError):
            approve_leave_request(
                db, lr.id, approver_id=manager.id, decision=LeaveStatus.APPROVED
            )

    def test_second_approval_leaves_used_days_unchanged(self, db):
        """CAS prevents double-deduction: used_days stays the same after second attempt."""
        manager = _make_employee(db, "Alice Manager", "alice_dbl2@test.com")
        employee = _make_employee(db, "Bob Employee", "bob_dbl2@test.com", manager=manager)
        bal = _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)
        bal_id = bal.id

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)
        )

        # First approval
        approve_leave_request(
            db, lr.id, approver_id=manager.id, decision=LeaveStatus.APPROVED
        )

        db.expire_all()
        after_first = db.query(LeaveBalance).filter(LeaveBalance.id == bal_id).first().used_days

        # Second approval attempt
        with pytest.raises(RequestNotPendingError):
            approve_leave_request(
                db, lr.id, approver_id=manager.id, decision=LeaveStatus.APPROVED
            )

        db.expire_all()
        after_second = db.query(LeaveBalance).filter(LeaveBalance.id == bal_id).first().used_days

        assert after_second == pytest.approx(after_first)


# ── Balance re-check ──────────────────────────────────────────────────────

class TestBalanceRecheck:
    def test_insufficient_balance_raises(self, db):
        """If balance dropped between submission and approval, raise InsufficientBalanceError."""
        manager = _make_employee(db, "Alice Manager", "alice_bal@test.com")
        employee = _make_employee(db, "Bob Employee", "bob_bal@test.com", manager=manager)
        bal = _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=3)

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 6, 1), date(2026, 6, 3)  # 3 working days
        )

        # Drain the balance so only 1 day remains
        bal.used_days = 2.0
        db.commit()

        with pytest.raises(InsufficientBalanceError):
            approve_leave_request(
                db, lr.id, approver_id=manager.id, decision=LeaveStatus.APPROVED
            )


# ── Year-spanning approval ────────────────────────────────────────────────

class TestYearSpanning:
    def test_year_spanning_creates_two_deductions(self, db):
        """A leave spanning two calendar years creates two LeaveDeduction rows."""
        manager = _make_employee(db, "Alice Manager", "alice_yr@test.com")
        employee = _make_employee(db, "Bob Employee", "bob_yr@test.com", manager=manager)
        _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)
        _make_balance(db, employee.id, LeaveType.ANNUAL, 2027, total_days=14)

        # Dec 28 2026 (Mon) to Jan 5 2027 (Tue)
        # Dec 28(Mon), 29(Tue), 30(Wed), 31(Thu) → 4 days in 2026
        # Jan 1(Thu), 2(Fri), (3-4 weekend), 5(Mon) → 3 days in 2027
        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 12, 28), date(2027, 1, 5)
        )

        approve_leave_request(
            db, lr.id, approver_id=manager.id, decision=LeaveStatus.APPROVED
        )

        deductions = db.query(LeaveDeduction).filter(
            LeaveDeduction.leave_request_id == lr.id
        ).all()
        assert len(deductions) == 2

        by_year = {d.year: d.days for d in deductions}
        assert 2026 in by_year
        assert 2027 in by_year
        assert by_year[2026] == pytest.approx(4.0)
        assert by_year[2027] == pytest.approx(3.0)

    def test_year_spanning_updates_two_balances(self, db):
        """A year-spanning approval updates used_days in both years."""
        manager = _make_employee(db, "Alice Manager", "alice_yr2@test.com")
        employee = _make_employee(db, "Bob Employee", "bob_yr2@test.com", manager=manager)
        bal_2026 = _make_balance(db, employee.id, LeaveType.ANNUAL, 2026, total_days=14)
        bal_2027 = _make_balance(db, employee.id, LeaveType.ANNUAL, 2027, total_days=14)
        bal_2026_id = bal_2026.id
        bal_2027_id = bal_2027.id

        lr = _make_pending_request(
            db, employee.id, LeaveType.ANNUAL,
            date(2026, 12, 28), date(2027, 1, 5)
        )

        approve_leave_request(
            db, lr.id, approver_id=manager.id, decision=LeaveStatus.APPROVED
        )

        db.expire_all()
        b2026 = db.query(LeaveBalance).filter(LeaveBalance.id == bal_2026_id).first()
        b2027 = db.query(LeaveBalance).filter(LeaveBalance.id == bal_2027_id).first()
        assert b2026.used_days == pytest.approx(4.0)
        assert b2027.used_days == pytest.approx(3.0)
