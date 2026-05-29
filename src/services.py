"""
Business logic layer for leave management.

Implement the following service functions to handle leave request workflows.
Each function should raise appropriate exceptions for invalid operations
(e.g., overlapping leave, insufficient balance, self-approval).
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from src.models import Employee, LeaveRequest, LeaveBalance, LeaveType, LeaveStatus


class LeaveError(Exception):
    pass


# ── Employee / Request lookup errors ─────────────────────────────────────────

class EmployeeNotFoundError(LeaveError):
    pass


class LeaveRequestNotFoundError(LeaveError):
    pass


# ── Date validation errors ────────────────────────────────────────────────────

class InvalidDateRangeError(LeaveError):
    pass


class BackdatedRequestError(LeaveError):
    pass


class UnknownLeaveTypeError(LeaveError):
    pass


class NoWorkingDaysError(LeaveError):
    pass


# ── Conflict errors ───────────────────────────────────────────────────────────

class OverlappingLeaveError(LeaveError):
    pass


class InsufficientBalanceError(LeaveError):
    pass


# ── Approval / authorization errors ──────────────────────────────────────────

class SelfApprovalError(LeaveError):
    pass


class NotAuthorizedApproverError(LeaveError):
    pass


class RequestNotPendingError(LeaveError):
    pass


# ── Cancellation errors ───────────────────────────────────────────────────────

class NotRequestOwnerError(LeaveError):
    pass


class AlreadyCancelledError(LeaveError):
    pass


class RejectedRequestNotCancellableError(LeaveError):
    pass


# ── Legacy alias ─────────────────────────────────────────────────────────────

class CannotModifyApprovedLeaveError(LeaveError):
    pass


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
    raise NotImplementedError("Candidate must implement this")


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
