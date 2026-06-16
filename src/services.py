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


def _get_employee_or_404(db: Session, employee_id: int) -> Employee:
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise LeaveError(f"Employee {employee_id} not found")
    return emp


def _get_leave_request_or_404(db: Session, leave_request_id: int) -> LeaveRequest:
    lr = db.query(LeaveRequest).filter(LeaveRequest.id == leave_request_id).first()
    if not lr:
        raise LeaveError(f"Leave request {leave_request_id} not found")
    return lr


def _count_working_days(start_date: date, end_date: date) -> float:
    """Count calendar days inclusive. Weekend-skipping left as a future enhancement
    (see DESIGN.md) - we use calendar days to keep the model simple and predictable."""
    return float((end_date - start_date).days + 1)


def _get_balance(
    db: Session, employee_id: int, leave_type: LeaveType, year: int
) -> LeaveBalance | None:
    return (
        db.query(LeaveBalance)
        .filter(
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.leave_type == leave_type,
            LeaveBalance.year == year,
        )
        .with_for_update()  # pessimistic lock to prevent concurrent over-deduction
        .first()
    )


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
    - end_date - start_date >= 0 (at least 1 day - or handle half-day logic)
    """
    employee = _get_employee_or_404(db, employee_id)  # 404 if employee doesn't exist

    today = date.today()
    if start_date < today:
        raise LeaveError("Leave start date cannot be in the past")
    if end_date < start_date:
        raise LeaveError("end_date must be on or after start_date")

    # Overlap check: any active (non-cancelled, non-rejected) request whose
    # date range intersects [start_date, end_date].
    overlap = (
        db.query(LeaveRequest)
        .filter(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.not_in([LeaveStatus.CANCELLED, LeaveStatus.REJECTED]),
            LeaveRequest.start_date <= end_date,
            LeaveRequest.end_date >= start_date,
        )
        .first()
    )
    if overlap:
        raise OverlappingLeaveError(
            f"Leave overlaps with existing request {overlap.id} "
            f"({overlap.start_date} – {overlap.end_date})"
        )

    days_requested = _count_working_days(start_date, end_date)

    # UNPAID leave doesn't consume a balance bucket
    if leave_type != LeaveType.UNPAID:
        year = start_date.year
        balance = _get_balance(db, employee_id, leave_type, year)
        remaining_days = balance.remaining_days if balance else 0.0
        if remaining_days < days_requested:
            raise InsufficientBalanceError(
                f"Insufficient balance: need {days_requested} day(s), "
                f"have {remaining_days} remaining"
            )

    lr = LeaveRequest(
        employee_id=employee_id,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        reason=reason,
        status=LeaveStatus.PENDING,
    )
    db.add(lr)
    db.commit()
    db.refresh(lr)
    return lr


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
    if decision not in (LeaveStatus.APPROVED, LeaveStatus.REJECTED):
        raise LeaveError("Decision must be 'approved' or 'rejected'")

    lr = _get_leave_request_or_404(db, leave_request_id)

    if lr.status != LeaveStatus.PENDING:
        raise LeaveError(f"Leave request is already {lr.status.value}, cannot review")

    if lr.employee_id == approver_id:
        raise SelfApprovalError("Employees cannot approve their own leave requests")

    employee = _get_employee_or_404(db, lr.employee_id)
    if employee.manager_id != approver_id:
        raise LeaveError(
            f"Approver {approver_id} is not the manager of employee {lr.employee_id}"
        )

    if decision == LeaveStatus.APPROVED:
        if lr.leave_type != LeaveType.UNPAID:
            year = lr.start_date.year
            # with_for_update prevents two concurrent approvals double-deducting
            balance = _get_balance(db, lr.employee_id, lr.leave_type, year)
            days = _count_working_days(lr.start_date, lr.end_date)
            remaining_days = balance.remaining_days if balance else 0.0
            if remaining_days < days:
                raise InsufficientBalanceError(
                    f"Insufficient balance at approval time: need {days}, "
                    f"have {remaining_days}"
                )
            balance.used_days += days

    lr.status = decision
    lr.approved_by = approver_id
    lr.approved_at = datetime.utcnow()
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
    lr = _get_leave_request_or_404(db, leave_request_id)

    if lr.employee_id != employee_id:
        raise LeaveError("Only the leave owner can cancel this request")

    if lr.status == LeaveStatus.CANCELLED:
        raise LeaveError("Leave request is already cancelled")

    if lr.status == LeaveStatus.REJECTED:
        raise LeaveError("Rejected leave requests cannot be cancelled")

    was_approved = lr.status == LeaveStatus.APPROVED

    lr.status = LeaveStatus.CANCELLED

    if was_approved and lr.leave_type != LeaveType.UNPAID:
        year = lr.start_date.year
        balance = _get_balance(db, employee_id, lr.leave_type, year)
        if balance:
            days = _count_working_days(lr.start_date, lr.end_date)
            balance.used_days = max(0.0, balance.used_days - days)

    db.commit()
    db.refresh(lr)
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
    q = db.query(LeaveRequest)

    if employee_id is not None:
        q = q.filter(LeaveRequest.employee_id == employee_id)
    if status is not None:
        q = q.filter(LeaveRequest.status == status)
    if leave_type is not None:
        q = q.filter(LeaveRequest.leave_type == leave_type)
    if from_date is not None:
        q = q.filter(LeaveRequest.end_date >= from_date)
    if to_date is not None:
        q = q.filter(LeaveRequest.start_date <= to_date)

    total = q.count()
    items = (
        q.order_by(LeaveRequest.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return items, total


def get_leave_balances(
    db: Session,
    employee_id: int,
    year: Optional[int] = None,
) -> list[LeaveBalance]:
    """
    Get leave balances for an employee for a given year (defaults to current year).
    """
    if year is None:
        year = date.today().year

    return (
        db.query(LeaveBalance)
        .filter(
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.year == year,
        )
        .all()
    )


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
