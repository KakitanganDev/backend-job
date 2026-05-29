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


class InsufficientBalanceError(LeaveError):
    pass


class OverlappingLeaveError(LeaveError):
    pass


class SelfApprovalError(LeaveError):
    pass


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
    query = db.query(LeaveRequest)

    if employee_id is not None:
        query = query.filter(LeaveRequest.employee_id == employee_id)
    if status is not None:
        query = query.filter(LeaveRequest.status == status)
    if leave_type is not None:
        query = query.filter(LeaveRequest.leave_type == leave_type)
    if from_date is not None:
        query = query.filter(LeaveRequest.start_date >= from_date)
    if to_date is not None:
        query = query.filter(LeaveRequest.end_date <= to_date)

    total_count = query.count()
    offset = (page - 1) * page_size
    items = query.offset(offset).limit(page_size).all()

    return items, total_count


def get_leave_balances(
    db: Session,
    employee_id: int,
    year: Optional[int] = None,
) -> list[LeaveBalance]:
    """
    Get leave balances for an employee for a given year (defaults to current year).
    For any LeaveType with no existing row, a synthesized row with zero days is returned.
    Synthesized rows are NOT persisted to the database.
    """
    if year is None:
        year = date.today().year

    existing_rows = (
        db.query(LeaveBalance)
        .filter(
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.year == year,
        )
        .all()
    )

    existing_types = {row.leave_type for row in existing_rows}

    synthesized = [
        LeaveBalance(
            employee_id=employee_id,
            leave_type=lt,
            year=year,
            total_days=0,
            used_days=0,
        )
        for lt in LeaveType
        if lt not in existing_types
    ]

    return existing_rows + synthesized


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
