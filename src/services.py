"""
Business logic for the leave management system.

Concurrency model
-----------------
Approval and cancellation acquire a pessimistic row lock on the
LeaveRequest row, then on the LeaveBalance row, before mutating either.
On Postgres this is a real `SELECT ... FOR UPDATE`. On SQLite the engine
is configured to open every transaction with `BEGIN IMMEDIATE` (see
`database.py`), which gives us the same "second writer waits" guarantee.

Lock ordering is fixed (LeaveRequest first, then LeaveBalance) on every
mutating path, so two concurrent calls cannot deadlock.

Calendar / day counting
-----------------------
Days are counted as working days (Mon-Fri excluding Malaysian federal
public holidays), with optional half-day flags on the start and end
boundaries. See `src/calendar.py`.

Year-spanning leaves attribute the entire balance deduction to the
*start* year. See DESIGN.md §4 for the alternative we considered.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.calendar import working_days_between
from src.database import escalate_to_immediate
from src.models import (
    Employee,
    LeaveBalance,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
)
from src.observability import get_logger

log = get_logger(__name__)

# Leave types that don't draw from a balance.
_NO_BALANCE_TYPES: set[LeaveType] = {LeaveType.UNPAID}

# Statuses that block overlapping new requests / count as "active".
_ACTIVE_STATUSES = (LeaveStatus.PENDING, LeaveStatus.APPROVED)


# ── Exceptions ───────────────────────────────────────────────────────────

class LeaveError(Exception):
    """Base for all leave domain errors. The HTTP layer maps these to 422."""


class EmployeeNotFoundError(LeaveError):
    pass


class LeaveRequestNotFoundError(LeaveError):
    pass


class InsufficientBalanceError(LeaveError):
    pass


class OverlappingLeaveError(LeaveError):
    pass


class SelfApprovalError(LeaveError):
    pass


class NotAuthorizedError(LeaveError):
    pass


class CannotModifyApprovedLeaveError(LeaveError):
    pass


class InvalidLeaveDatesError(LeaveError):
    pass


class NoWorkingDaysError(LeaveError):
    pass


class BalanceNotAllocatedError(LeaveError):
    pass


# ── Helpers ──────────────────────────────────────────────────────────────

def _get_employee_or_raise(db: Session, employee_id: int) -> Employee:
    emp = db.get(Employee, employee_id)
    if emp is None:
        raise EmployeeNotFoundError(f"Employee {employee_id} not found")
    return emp


def _lock_balance_row(
    db: Session, employee_id: int, leave_type: LeaveType, year: int
) -> LeaveBalance | None:
    """Acquire a row lock on the balance row, or return None if absent.

    Returns None for leave types that don't track a balance (e.g. UNPAID).
    """
    if leave_type in _NO_BALANCE_TYPES:
        return None

    stmt = (
        select(LeaveBalance)
        .where(
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.leave_type == leave_type,
            LeaveBalance.year == year,
        )
        .with_for_update()
    )
    return db.execute(stmt).scalar_one_or_none()


def _has_overlap(
    db: Session,
    employee_id: int,
    start: date,
    end: date,
    *,
    exclude_id: int | None = None,
) -> bool:
    """True iff this employee already has an active request overlapping [start, end]."""
    stmt = select(LeaveRequest.id).where(
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.status.in_(_ACTIVE_STATUSES),
        # Standard interval overlap: A.start <= B.end AND B.start <= A.end.
        LeaveRequest.start_date <= end,
        LeaveRequest.end_date >= start,
    )
    if exclude_id is not None:
        stmt = stmt.where(LeaveRequest.id != exclude_id)
    return db.execute(stmt.limit(1)).first() is not None


# ── Service functions ────────────────────────────────────────────────────

def create_leave_request(
    db: Session,
    employee_id: int,
    leave_type: LeaveType,
    start_date: date,
    end_date: date,
    reason: str | None = None,
    *,
    start_half_day: bool = False,
    end_half_day: bool = False,
) -> LeaveRequest:
    """Create a new pending leave request.

    Validates: employee exists, dates well-ordered and not back-dated,
    no overlap with active requests, working-day count > 0, and (for
    balance-tracked types) sufficient remaining balance.

    Note: balance is *not* deducted here — only on approval. We still
    check it at submission time for fast user feedback.
    """
    if start_date > end_date:
        raise InvalidLeaveDatesError("start_date must be on or before end_date")
    if start_date < date.today():
        raise InvalidLeaveDatesError("Cannot request leave starting in the past")

    escalate_to_immediate(db)
    emp = _get_employee_or_raise(db, employee_id)

    days = working_days_between(
        start_date, end_date,
        start_half_day=start_half_day, end_half_day=end_half_day,
    )
    if days <= 0:
        raise NoWorkingDaysError(
            "Leave request must span at least one working day "
            "(weekends and public holidays don't count)"
        )

    # Lock the balance row first to serialize concurrent submissions for
    # the same (employee, type, year) — second call's overlap check then
    # sees the first call's row.
    year = start_date.year
    balance = _lock_balance_row(db, employee_id, leave_type, year)

    if leave_type not in _NO_BALANCE_TYPES:
        if balance is None:
            raise BalanceNotAllocatedError(
                f"No {leave_type.value} balance allocated for {year}"
            )
        if balance.remaining_days < days:
            raise InsufficientBalanceError(
                f"Need {days} days but only {balance.remaining_days} remaining"
            )

    if _has_overlap(db, employee_id, start_date, end_date):
        raise OverlappingLeaveError(
            "An active leave request already covers part of this date range"
        )

    lr = LeaveRequest(
        employee_id=employee_id,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        start_half_day=start_half_day,
        end_half_day=end_half_day,
        working_days=days,
        reason=reason,
        status=LeaveStatus.PENDING,
    )
    db.add(lr)
    db.commit()
    db.refresh(lr)

    log.info(
        "leave_request_created",
        request_id=lr.id, employee_id=emp.id,
        leave_type=leave_type.value, days=days,
    )
    return lr


def approve_leave_request(
    db: Session,
    leave_request_id: int,
    approver_id: int,
    decision: LeaveStatus,
) -> LeaveRequest:
    """Approve or reject a pending leave request.

    Validates: request exists and is PENDING, approver is not the
    requester, approver is the requester's direct manager. On approval,
    deducts working_days from the balance. The deduction is atomic with
    the status transition: a second concurrent approver finds the
    request no longer PENDING and raises CannotModifyApprovedLeaveError.
    """
    if decision not in (LeaveStatus.APPROVED, LeaveStatus.REJECTED):
        raise InvalidLeaveDatesError("decision must be 'approved' or 'rejected'")

    escalate_to_immediate(db)

    # Lock the request row first.
    stmt = (
        select(LeaveRequest)
        .where(LeaveRequest.id == leave_request_id)
        .with_for_update()
    )
    lr = db.execute(stmt).scalar_one_or_none()
    if lr is None:
        raise LeaveRequestNotFoundError(f"Leave request {leave_request_id} not found")
    if lr.status != LeaveStatus.PENDING:
        raise CannotModifyApprovedLeaveError(
            f"Cannot review leave in status {lr.status.value}"
        )

    if lr.employee_id == approver_id:
        raise SelfApprovalError("An employee cannot approve their own leave")

    requester = _get_employee_or_raise(db, lr.employee_id)
    if requester.manager_id != approver_id:
        raise NotAuthorizedError(
            f"Only the requester's manager (id={requester.manager_id}) can review"
        )

    if decision == LeaveStatus.APPROVED:
        balance = _lock_balance_row(
            db, lr.employee_id, lr.leave_type, lr.start_date.year
        )
        if balance is not None:
            if balance.remaining_days < lr.working_days:
                raise InsufficientBalanceError(
                    f"Need {lr.working_days} days but only {balance.remaining_days} remaining"
                )
            balance.used_days += lr.working_days

    lr.status = decision
    lr.approved_by = approver_id
    lr.approved_at = datetime.now(UTC)
    db.commit()
    db.refresh(lr)

    log.info(
        "leave_request_reviewed",
        request_id=lr.id, decision=decision.value, approver_id=approver_id,
    )
    return lr


def cancel_leave_request(
    db: Session,
    leave_request_id: int,
    employee_id: int,
) -> LeaveRequest:
    """Cancel a PENDING or APPROVED leave request.

    Owner-only. If the request was APPROVED, restores `working_days` to
    the balance under the same lock discipline as approval.
    """
    escalate_to_immediate(db)

    stmt = (
        select(LeaveRequest)
        .where(LeaveRequest.id == leave_request_id)
        .with_for_update()
    )
    lr = db.execute(stmt).scalar_one_or_none()
    if lr is None:
        raise LeaveRequestNotFoundError(f"Leave request {leave_request_id} not found")

    if lr.employee_id != employee_id:
        raise NotAuthorizedError("Only the leave owner can cancel")

    if lr.status not in (LeaveStatus.PENDING, LeaveStatus.APPROVED):
        raise CannotModifyApprovedLeaveError(
            f"Cannot cancel leave in status {lr.status.value}"
        )

    if lr.status == LeaveStatus.APPROVED:
        balance = _lock_balance_row(
            db, lr.employee_id, lr.leave_type, lr.start_date.year
        )
        if balance is not None:
            balance.used_days = max(0.0, balance.used_days - lr.working_days)

    lr.status = LeaveStatus.CANCELLED
    db.commit()
    db.refresh(lr)

    log.info("leave_request_cancelled", request_id=lr.id, employee_id=employee_id)
    return lr


def get_leave_requests(
    db: Session,
    employee_id: int | None = None,
    status: LeaveStatus | None = None,
    leave_type: LeaveType | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[LeaveRequest], int]:
    """List leave requests with filtering and offset pagination.

    `from_date` / `to_date` filter by date-range *overlap*: a request
    matches if its [start_date, end_date] intersects [from_date, to_date].
    Returns (items, total_count) — total ignores pagination so the UI
    can render page links.
    """
    if page < 1:
        raise InvalidLeaveDatesError("page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise InvalidLeaveDatesError("page_size must be between 1 and 100")

    filters = []
    if employee_id is not None:
        filters.append(LeaveRequest.employee_id == employee_id)
    if status is not None:
        filters.append(LeaveRequest.status == status)
    if leave_type is not None:
        filters.append(LeaveRequest.leave_type == leave_type)
    if from_date is not None:
        filters.append(LeaveRequest.end_date >= from_date)
    if to_date is not None:
        filters.append(LeaveRequest.start_date <= to_date)

    total = db.query(LeaveRequest).filter(*filters).count()

    stmt = (
        select(LeaveRequest)
        .where(*filters)
        .order_by(LeaveRequest.start_date.desc(), LeaveRequest.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = list(db.execute(stmt).scalars().all())
    return items, total


def get_leave_balances(
    db: Session,
    employee_id: int,
    year: int | None = None,
) -> list[LeaveBalance]:
    """Get all leave balances for an employee in a given year.

    Defaults to the current calendar year.
    """
    if year is None:
        year = date.today().year
    stmt = (
        select(LeaveBalance)
        .where(
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.year == year,
        )
        .order_by(LeaveBalance.leave_type)
    )
    return list(db.execute(stmt).scalars().all())


# ── Demo seeding ─────────────────────────────────────────────────────────

def seed_demo_data(db: Session) -> None:
    """Seed a few employees and balances. Idempotent — no-op if data exists."""
    if db.query(Employee).first() is not None:
        return

    alice = Employee(name="Alice Manager", email="alice@company.com", department="Engineering")
    db.add(alice)
    db.flush()

    bob = Employee(
        name="Bob Engineer", email="bob@company.com",
        department="Engineering", manager_id=alice.id,
    )
    carol = Employee(
        name="Carol Engineer", email="carol@company.com",
        department="Engineering", manager_id=alice.id,
    )
    db.add_all([bob, carol])
    db.flush()

    year = date.today().year
    db.add_all([
        LeaveBalance(employee_id=bob.id, leave_type=LeaveType.ANNUAL, year=year, total_days=14),
        LeaveBalance(employee_id=bob.id, leave_type=LeaveType.SICK, year=year, total_days=12),
        LeaveBalance(employee_id=carol.id, leave_type=LeaveType.ANNUAL, year=year, total_days=14),
        LeaveBalance(employee_id=carol.id, leave_type=LeaveType.SICK, year=year, total_days=12),
    ])
    db.commit()
