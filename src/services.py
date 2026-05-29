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


class LeaveRequestNotFoundError(LeaveError):
    pass


class NotRequestOwnerError(LeaveError):
    pass


class AlreadyCancelledError(LeaveError):
    pass


class RejectedRequestNotCancellableError(LeaveError):
    pass


class RequestNotPendingError(LeaveError):
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
    from src.models import LeaveDeduction
    from sqlalchemy import update as sa_update, or_

    # 1. Load request
    lr = db.query(LeaveRequest).filter(LeaveRequest.id == leave_request_id).first()
    if not lr:
        raise LeaveRequestNotFoundError(f"leave request {leave_request_id} not found")

    # 2. Ownership check
    if lr.employee_id != employee_id:
        raise NotRequestOwnerError("only the request owner can cancel this leave")

    # 3. Terminal state checks
    if lr.status == LeaveStatus.CANCELLED:
        raise AlreadyCancelledError("this request is already cancelled")
    if lr.status == LeaveStatus.REJECTED:
        raise RejectedRequestNotCancellableError("rejected requests cannot be cancelled")

    # 4. Record prior status
    prior_status = lr.status

    # 5. CAS UPDATE — atomic compare-and-swap from pending OR approved to cancelled
    stmt = (
        sa_update(LeaveRequest)
        .where(
            LeaveRequest.id == leave_request_id,
            or_(
                LeaveRequest.status == LeaveStatus.PENDING,
                LeaveRequest.status == LeaveStatus.APPROVED,
            )
        )
        .values(status=LeaveStatus.CANCELLED)
    )
    result = db.execute(stmt)
    if result.rowcount == 0:
        # Someone else cancelled it concurrently
        db.rollback()
        raise AlreadyCancelledError("this request is already cancelled")

    # 6. Balance restoration (only if prior_status was APPROVED)
    deduction_rows = []
    if prior_status == LeaveStatus.APPROVED:
        deduction_rows = (
            db.query(LeaveDeduction)
            .filter(LeaveDeduction.leave_request_id == leave_request_id)
            .all()
        )
        for deduction in deduction_rows:
            balance = (
                db.query(LeaveBalance)
                .filter(
                    LeaveBalance.employee_id == lr.employee_id,
                    LeaveBalance.leave_type == lr.leave_type,
                    LeaveBalance.year == deduction.year,
                )
                .first()
            )
            if balance:
                balance.used_days = balance.used_days - deduction.days
            db.delete(deduction)

    # 7. Commit and return
    db.commit()
    db.refresh(lr)
    if prior_status == LeaveStatus.APPROVED:
        lr._restored_deductions = [{"year": d.year, "days": d.days} for d in deduction_rows]
    else:
        lr._restored_deductions = []
    return lr


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
