"""
Business logic layer for leave management.

Implement the following service functions to handle leave request workflows.
Each function should raise appropriate exceptions for invalid operations
(e.g., overlapping leave, insufficient balance, self-approval).
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from src.models import Employee, LeaveRequest, LeaveBalance, LeaveType, LeaveStatus
from src.leave_math import partition_by_year


# ── Exceptions ────────────────────────────────────────────────────────────

class LeaveError(Exception):
    pass


class InsufficientBalanceError(LeaveError):
    pass


class OverlappingLeaveError(LeaveError):
    pass


class SelfApprovalError(LeaveError):
    pass


class CannotModifyApprovedLeaveError(LeaveError):
    pass


class RequestNotPendingError(LeaveError):
    pass


class NotAuthorizedApproverError(LeaveError):
    pass


class LeaveRequestNotFoundError(LeaveError):
    pass


# ── Helpers ───────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    """Return current UTC datetime. Overridable in tests via monkeypatch."""
    return datetime.utcnow()


# ── Service functions ─────────────────────────────────────────────────────

def create_leave_request(
    db: Session,
    employee_id: int,
    leave_type: LeaveType,
    start_date: date,
    end_date: date,
    reason: Optional[str] = None,
) -> LeaveRequest:
    """
    Create a new leave request.
    Must validate:
    - Employee exists
    - start_date <= end_date
    - start_date >= today (no back-dating)
    - No overlapping leave requests for the same employee
    - Employee has sufficient leave balance for the requested type
    - end_date - start_date >= 0 (at least 1 day — or handle half-day logic)
    """
    raise NotImplementedError("Candidate must implement this")


def approve_leave_request(
    db: Session,
    leave_request_id: int,
    approver_id: int,
    decision: LeaveStatus,
) -> LeaveRequest:
    """
    Approve or reject a pending leave request.
    Must validate:
    - Leave request exists and is in PENDING status
    - Approver is the employee's manager (or has approval authority)
    - Approver is not the leave requester (no self-approval)
    - On approval: deduct from leave balance
    - On rejection: record reason in comment field if needed
    """
    from src.models import LeaveDeduction, Holiday

    # 1. Load request
    lr = db.query(LeaveRequest).filter(LeaveRequest.id == leave_request_id).first()
    if not lr:
        raise LeaveRequestNotFoundError(f"leave request {leave_request_id} not found")

    # Load employee
    employee = db.query(Employee).filter(Employee.id == lr.employee_id).first()

    # 2. Authorization checks
    if approver_id == lr.employee_id:
        raise SelfApprovalError("you cannot approve your own leave request")

    if employee.manager_id != approver_id:
        raise NotAuthorizedApproverError("you are not authorized to approve this request")

    # 3. Status check (pre-flight)
    if lr.status != LeaveStatus.PENDING:
        raise RequestNotPendingError("this request is no longer pending")

    if decision == LeaveStatus.APPROVED:
        # 4. Balance re-check
        # Query holidays for the date range
        try:
            holiday_rows = (
                db.query(Holiday)
                .filter(Holiday.date >= lr.start_date, Holiday.date <= lr.end_date)
                .all()
            )
            holidays_set = {h.date for h in holiday_rows}
        except Exception:
            holidays_set = set()

        deductions = partition_by_year(
            lr.start_date, lr.end_date,
            lr.half_day_start, lr.half_day_end,
            holidays_set,
        )

        for year, days in deductions.items():
            balance = (
                db.query(LeaveBalance)
                .filter(
                    LeaveBalance.employee_id == lr.employee_id,
                    LeaveBalance.leave_type == lr.leave_type,
                    LeaveBalance.year == year,
                )
                .first()
            )
            remaining = balance.remaining_days if balance else 0.0
            if remaining < days:
                raise InsufficientBalanceError(
                    f"{employee.name} no longer has enough balance for this request"
                )

        # 5. CAS UPDATE — atomic compare-and-swap
        stmt = (
            sa_update(LeaveRequest)
            .where(
                LeaveRequest.id == leave_request_id,
                LeaveRequest.status == LeaveStatus.PENDING,
            )
            .values(
                status=LeaveStatus.APPROVED,
                approved_by=approver_id,
                approved_at=_now_utc(),
            )
        )
        result = db.execute(stmt)

        if result.rowcount == 0:
            db.rollback()
            raise RequestNotPendingError("this request is no longer pending")

        # 6. Balance deductions (in the same transaction)
        for year, days in deductions.items():
            # Update leave_balances
            balance = (
                db.query(LeaveBalance)
                .filter(
                    LeaveBalance.employee_id == lr.employee_id,
                    LeaveBalance.leave_type == lr.leave_type,
                    LeaveBalance.year == year,
                )
                .first()
            )
            if balance:
                balance.used_days = balance.used_days + days

            # Insert leave_deductions
            db.add(LeaveDeduction(
                leave_request_id=leave_request_id,
                year=year,
                days=days,
            ))

        # 7. Commit and refresh
        db.commit()
        db.refresh(lr)
        lr._deductions = [{"year": yr, "days": d} for yr, d in deductions.items()]

    elif decision == LeaveStatus.REJECTED:
        # CAS UPDATE for rejection
        stmt = (
            sa_update(LeaveRequest)
            .where(
                LeaveRequest.id == leave_request_id,
                LeaveRequest.status == LeaveStatus.PENDING,
            )
            .values(
                status=LeaveStatus.REJECTED,
                approved_by=approver_id,
                approved_at=_now_utc(),
            )
        )
        result = db.execute(stmt)

        if result.rowcount == 0:
            db.rollback()
            raise RequestNotPendingError("this request is no longer pending")

        db.commit()
        db.refresh(lr)

    return lr


def cancel_leave_request(
    db: Session,
    leave_request_id: int,
    employee_id: int,
) -> LeaveRequest:
    """
    Cancel a leave request.
    - Only the owner can cancel
    - Can only cancel PENDING or APPROVED leaves
    - Cancelling an approved leave restores balance
    """
    raise NotImplementedError("Candidate must implement this")


def get_leave_requests(
    db: Session,
    employee_id: Optional[int] = None,
    status: Optional[LeaveStatus] = None,
    leave_type: Optional[LeaveType] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[LeaveRequest], int]:
    """
    List leave requests with filtering and pagination.
    Returns (items, total_count).
    """
    raise NotImplementedError("Candidate must implement this")


def get_leave_balances(
    db: Session,
    employee_id: int,
    year: Optional[int] = None,
) -> list[LeaveBalance]:
    """
    Get leave balances for an employee for a given year (defaults to current year).
    """
    raise NotImplementedError("Candidate must implement this")


def seed_demo_data(db: Session) -> None:
    """Seed database with demo employees and leave balances for testing."""
    from src.models import LeaveType, LeaveBalance

    existing = db.query(Employee).first()
    if existing:
        return

    alice = Employee(name="Alice Manager", email="alice@company.com", department="Engineering")
    bob = Employee(name="Bob Engineer", email="bob@company.com", department="Engineering", manager=alice)
    carol = Employee(name="Carol Engineer", email="carol@company.com", department="Engineering", manager=alice)
    db.add_all([alice, bob, carol])
    db.flush()

    year = date.today().year
    balances = [
        LeaveBalance(employee_id=bob.id, leave_type=LeaveType.ANNUAL, year=year, total_days=14),
        LeaveBalance(employee_id=bob.id, leave_type=LeaveType.SICK, year=year, total_days=12),
        LeaveBalance(employee_id=carol.id, leave_type=LeaveType.ANNUAL, year=year, total_days=14),
        LeaveBalance(employee_id=carol.id, leave_type=LeaveType.SICK, year=year, total_days=12),
    ]
    db.add_all(balances)
    db.commit()
