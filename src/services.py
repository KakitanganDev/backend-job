"""
Business logic layer for leave management.

Implements leave request workflows with:
- Validation (backdating, date range, working days, balance, overlap)
- CAS-based atomic approval to prevent double-deduction
- Balance restoration on cancellation
- Year-boundary split for multi-year requests
- Public holiday and weekend exclusion
"""

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from src.models import (
    Employee,
    Holiday,
    LeaveBalance,
    LeaveDeduction,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
)


# ── Today helper (monkeypatchable in tests) ───────────────────────────────────


def _today() -> date:
    return date.today()


# ── Exception hierarchy ───────────────────────────────────────────────────────


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


class BackdatedRequestError(LeaveError):
    pass


class InvalidDateRangeError(LeaveError):
    pass


class NoWorkingDaysError(LeaveError):
    pass


class RequestNotPendingError(LeaveError):
    pass


class NotRequestOwnerError(LeaveError):
    pass


class AlreadyCancelledError(LeaveError):
    pass


class RejectedRequestNotCancellableError(LeaveError):
    pass


# ── Leave math helpers ────────────────────────────────────────────────────────


def _get_holidays(db: Session, start: date, end: date) -> set:
    """Return set of holiday dates between start and end (inclusive)."""
    rows = db.query(Holiday).filter(
        Holiday.date >= start,
        Holiday.date <= end,
    ).all()
    return {row.date for row in rows}


def _count_working_days(start: date, end: date, holidays: set) -> float:
    """Count weekdays (Mon-Fri) that are not public holidays."""
    count = 0.0
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holidays:  # Mon=0 … Fri=4
            count += 1.0
        current += timedelta(days=1)
    return count


def _compute_deductions(
    db: Session,
    start: date,
    end: date,
    half_day_start: bool = False,
) -> list[dict]:
    """
    Compute leave day deductions split by calendar year.

    Returns a list of {"year": int, "days": float}, one entry per year touched.
    Weekends and public holidays are excluded.
    If half_day_start is True, the start date counts as 0.5 instead of 1.0.
    """
    holidays = _get_holidays(db, start, end)

    # Split the range by year
    results = []
    current_year_start = start

    while current_year_start.year <= end.year:
        year = current_year_start.year
        year_end = min(end, date(year, 12, 31))

        days = _count_working_days(current_year_start, year_end, holidays)

        # Apply half-day adjustment to the first day of the entire range
        if half_day_start and year == start.year:
            # If start day is a working day, reduce by 0.5
            if start.weekday() < 5 and start not in holidays:
                days -= 0.5

        if days > 0:
            results.append({"year": year, "days": days})

        # Advance to next year
        if year < end.year:
            current_year_start = date(year + 1, 1, 1)
        else:
            break

    return results


# ── Service functions ─────────────────────────────────────────────────────────


def create_leave_request(
    db: Session,
    employee_id: int,
    leave_type: LeaveType,
    start_date: date,
    end_date: date,
    reason: Optional[str] = None,
    half_day_start: bool = False,
) -> LeaveRequest:
    """
    Create a new leave request.

    Validates:
    - start_date <= end_date
    - start_date >= today (no back-dating)
    - At least one working day in the range
    - No overlapping pending/approved leave requests for the same employee
    - Employee has sufficient balance for the year(s) covered

    Sets _estimated_deductions on the returned object (transient attribute).
    """
    today = _today()

    # Validate date range
    if end_date < start_date:
        raise InvalidDateRangeError(
            f"end_date {end_date} is before start_date {start_date}"
        )

    # No backdating
    if start_date < today:
        raise BackdatedRequestError(
            f"start_date {start_date} is in the past (today is {today})"
        )

    # Compute working-day deductions
    deductions = _compute_deductions(db, start_date, end_date, half_day_start=half_day_start)
    if not deductions or sum(d["days"] for d in deductions) == 0:
        raise NoWorkingDaysError(
            f"Date range {start_date} to {end_date} contains no working days"
        )

    # Check for overlapping active requests (pending or approved)
    overlap = (
        db.query(LeaveRequest)
        .filter(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_([LeaveStatus.PENDING, LeaveStatus.APPROVED]),
            LeaveRequest.start_date <= end_date,
            LeaveRequest.end_date >= start_date,
        )
        .first()
    )
    if overlap:
        raise OverlappingLeaveError(
            f"Overlaps with existing leave request {overlap.id} "
            f"({overlap.start_date} to {overlap.end_date})"
        )

    # Check balance for each year
    for deduction in deductions:
        year = deduction["year"]
        days_needed = deduction["days"]
        balance = (
            db.query(LeaveBalance)
            .filter(
                LeaveBalance.employee_id == employee_id,
                LeaveBalance.leave_type == leave_type,
                LeaveBalance.year == year,
            )
            .first()
        )
        if balance is None or (balance.total_days - balance.used_days) < days_needed:
            available = 0.0 if balance is None else (balance.total_days - balance.used_days)
            raise InsufficientBalanceError(
                f"Insufficient {leave_type} balance for {year}: "
                f"need {days_needed}, have {available}"
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

    # Attach deductions as a transient attribute (not persisted here)
    lr._estimated_deductions = deductions
    return lr


def approve_leave_request(
    db: Session,
    leave_request_id: int,
    approver_id: int,
    decision: LeaveStatus,
) -> LeaveRequest:
    """
    Approve or reject a pending leave request using Compare-and-Swap.

    For approval:
    - Re-checks balance at approval time (balance could have changed since submission)
    - Atomically transitions PENDING → APPROVED and records deductions
    - Raises RequestNotPendingError if the request is no longer pending
      (handles concurrent approval attempts)

    For rejection:
    - Atomically transitions PENDING → REJECTED
    """
    # Use a subquery-based CAS: UPDATE ... WHERE status = PENDING
    # We read the request, validate, then update only if still pending
    lr = db.query(LeaveRequest).filter(LeaveRequest.id == leave_request_id).first()
    if lr is None:
        raise LeaveError(f"Leave request {leave_request_id} not found")

    if lr.status != LeaveStatus.PENDING:
        raise RequestNotPendingError(
            f"Leave request {leave_request_id} is not pending (status={lr.status})"
        )

    if lr.employee_id == approver_id:
        raise SelfApprovalError(
            f"Employee {approver_id} cannot approve their own leave request"
        )

    if decision == LeaveStatus.APPROVED:
        # Recompute deductions at approval time
        deductions = _compute_deductions(db, lr.start_date, lr.end_date)

        # Re-check balance at approval time and verify sufficient
        for deduction in deductions:
            year = deduction["year"]
            days_needed = deduction["days"]
            balance = (
                db.query(LeaveBalance)
                .filter(
                    LeaveBalance.employee_id == lr.employee_id,
                    LeaveBalance.leave_type == lr.leave_type,
                    LeaveBalance.year == year,
                )
                .first()
            )
            if balance is None or (balance.total_days - balance.used_days) < days_needed:
                available = 0.0 if balance is None else (balance.total_days - balance.used_days)
                raise InsufficientBalanceError(
                    f"Insufficient {lr.leave_type} balance for {year} at approval: "
                    f"need {days_needed}, have {available}"
                )

        # CAS: use UPDATE with WHERE status = PENDING to prevent double-approval
        from sqlalchemy import update
        rows_updated = db.execute(
            update(LeaveRequest)
            .where(
                and_(
                    LeaveRequest.id == leave_request_id,
                    LeaveRequest.status == LeaveStatus.PENDING,
                )
            )
            .values(
                status=LeaveStatus.APPROVED,
                approved_by=approver_id,
                approved_at=datetime.utcnow(),
            )
        ).rowcount
        db.flush()

        if rows_updated == 0:
            # Another thread already approved this request
            db.rollback()
            raise RequestNotPendingError(
                f"Leave request {leave_request_id} was already processed by another thread"
            )

        # Deduct balance and record deduction rows
        for deduction in deductions:
            year = deduction["year"]
            days = deduction["days"]
            balance = (
                db.query(LeaveBalance)
                .filter(
                    LeaveBalance.employee_id == lr.employee_id,
                    LeaveBalance.leave_type == lr.leave_type,
                    LeaveBalance.year == year,
                )
                .first()
            )
            balance.used_days += days
            db.add(LeaveDeduction(
                leave_request_id=leave_request_id,
                year=year,
                days=days,
            ))

        db.commit()
    else:
        # Rejection: CAS transition
        from sqlalchemy import update
        rows_updated = db.execute(
            update(LeaveRequest)
            .where(
                and_(
                    LeaveRequest.id == leave_request_id,
                    LeaveRequest.status == LeaveStatus.PENDING,
                )
            )
            .values(
                status=decision,
                approved_by=approver_id,
                approved_at=datetime.utcnow(),
            )
        ).rowcount
        db.flush()

        if rows_updated == 0:
            db.rollback()
            raise RequestNotPendingError(
                f"Leave request {leave_request_id} is no longer pending"
            )
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

    Rules:
    - Only the owner can cancel
    - Cancelled requests cannot be cancelled again (AlreadyCancelledError)
    - Rejected requests cannot be cancelled (RejectedRequestNotCancellableError)
    - Cancelling an approved leave restores balance by deleting deduction rows
      and reversing used_days

    Sets _restored_deductions on the returned object (transient attribute, list of dicts).
    """
    lr = db.query(LeaveRequest).filter(LeaveRequest.id == leave_request_id).first()
    if lr is None:
        raise LeaveError(f"Leave request {leave_request_id} not found")

    if lr.employee_id != employee_id:
        raise NotRequestOwnerError(
            f"Employee {employee_id} does not own leave request {leave_request_id}"
        )

    if lr.status == LeaveStatus.CANCELLED:
        raise AlreadyCancelledError(
            f"Leave request {leave_request_id} is already cancelled"
        )

    if lr.status == LeaveStatus.REJECTED:
        raise RejectedRequestNotCancellableError(
            f"Leave request {leave_request_id} is rejected and cannot be cancelled"
        )

    restored = []

    if lr.status == LeaveStatus.APPROVED:
        # Load deduction rows and restore balances
        deductions = (
            db.query(LeaveDeduction)
            .filter(LeaveDeduction.leave_request_id == leave_request_id)
            .all()
        )
        for ded in deductions:
            balance = (
                db.query(LeaveBalance)
                .filter(
                    LeaveBalance.employee_id == lr.employee_id,
                    LeaveBalance.leave_type == lr.leave_type,
                    LeaveBalance.year == ded.year,
                )
                .first()
            )
            if balance is not None:
                balance.used_days -= ded.days
            restored.append({"year": ded.year, "days": ded.days})
            db.delete(ded)

    lr.status = LeaveStatus.CANCELLED
    db.commit()
    db.refresh(lr)

    lr._restored_deductions = restored
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
    List leave requests with optional filtering and pagination.
    Returns (items, total_count) where total_count is the full matching count.
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
        year = _today().year

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
    existing = db.query(Employee).first()
    if existing:
        return

    alice = Employee(name="Alice Manager", email="alice@company.com", department="Engineering")
    bob = Employee(name="Bob Engineer", email="bob@company.com", department="Engineering", manager=alice)
    carol = Employee(name="Carol Engineer", email="carol@company.com", department="Engineering", manager=alice)
    david = Employee(name="David Engineer", email="david@company.com", department="Engineering", manager=alice)
    db.add_all([alice, bob, carol, david])
    db.flush()

    year = _today().year
    balances = [
        LeaveBalance(employee_id=bob.id, leave_type=LeaveType.ANNUAL, year=year, total_days=14),
        LeaveBalance(employee_id=bob.id, leave_type=LeaveType.SICK, year=year, total_days=12),
        LeaveBalance(employee_id=carol.id, leave_type=LeaveType.ANNUAL, year=year, total_days=14),
        LeaveBalance(employee_id=carol.id, leave_type=LeaveType.SICK, year=year, total_days=12),
    ]
    db.add_all(balances)
    db.commit()
