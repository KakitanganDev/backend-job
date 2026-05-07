"""
Business logic layer for leave management.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from src.models import (
    Employee,
    LeaveBalance,
    LeaveDuration,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    PublicHoliday,
)

logger = logging.getLogger(__name__)


# ── Exceptions ─────────────────────────────────────────────────────────────

class LeaveError(Exception):
    pass


class InsufficientBalanceError(LeaveError):
    pass


class OverlappingLeaveError(LeaveError):
    pass


class SelfReviewError(LeaveError):
    pass


class AlreadyReviewedError(LeaveError):
    pass


class NotDirectManagerError(LeaveError):
    pass


class NotFoundError(LeaveError):
    pass


class UnauthorizedAccessError(LeaveError):
    pass


# ── Utility ────────────────────────────────────────────────────────────────

def _today() -> date:
    """Return today's date.  Factored out so tests can freeze time."""
    return date.today()


def count_working_days(
    db: Session,
    start_date: date,
    end_date: date,
    duration: LeaveDuration,
) -> float:
    """Count working days in [start_date, end_date],
    excluding weekends and public holidays."""
    if duration in (LeaveDuration.FIRST_HALF, LeaveDuration.SECOND_HALF):
        holiday = (
            db.query(PublicHoliday)
            .filter(PublicHoliday.date == start_date)
            .first()
        )
        return 0.0 if holiday else 0.5

    holiday_dates = {
        row[0]
        for row in db.query(PublicHoliday.date)
        .filter(PublicHoliday.date >= start_date, PublicHoliday.date <= end_date)
        .all()
    }

    current = start_date
    count = 0
    while current <= end_date:
        if current.weekday() < 5 and current not in holiday_dates:
            count += 1
        current += timedelta(days=1)

    return float(count)


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
        .first()
    )


# ── Leave Requests ─────────────────────────────────────────────────────────

def create_leave_request(
    db: Session,
    employee_id: int,
    leave_type: LeaveType,
    start_date: date,
    end_date: date,
    duration: LeaveDuration = LeaveDuration.FULL,
    reason: str | None = None,
) -> LeaveRequest:
    today = _today()

    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise NotFoundError("Employee not found")

    if start_date < today:
        raise LeaveError("Cannot back-date leave requests")

    if start_date > end_date:
        raise LeaveError("Start date must be before or equal to end date")

    if start_date.year != end_date.year:
        raise LeaveError(
            "Cross-year leave is not supported. "
            "Please submit separate requests for each year."
        )

    if duration in (LeaveDuration.FIRST_HALF, LeaveDuration.SECOND_HALF):
        if start_date != end_date:
            raise LeaveError("Half-day leave must be a single day")
        if start_date.weekday() >= 5:
            raise LeaveError("Cannot take half-day leave on a non-working day")

    # Overlap check — all types and durations, only pending/approved
    overlapping = (
        db.query(LeaveRequest)
        .filter(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_([LeaveStatus.PENDING, LeaveStatus.APPROVED]),
            LeaveRequest.start_date <= end_date,
            LeaveRequest.end_date >= start_date,
        )
        .first()
    )
    if overlapping:
        # Same-day half-day collision → specific message
        if (
            duration in (LeaveDuration.FIRST_HALF, LeaveDuration.SECOND_HALF)
            and overlapping.duration in (
                LeaveDuration.FIRST_HALF, LeaveDuration.SECOND_HALF
            )
            and start_date == overlapping.start_date
        ):
            raise OverlappingLeaveError(
                "You already have a half-day leave on this date. "
                "Cancel the existing half-day request "
                "and create a full-day request instead."
            )
        raise OverlappingLeaveError(
            "Leave request overlaps with an existing pending or approved request"
        )

    year = start_date.year

    # Working days to deduct
    requested_days = count_working_days(db, start_date, end_date, duration)
    if requested_days == 0:
        raise LeaveError("No working days in the requested date range")

    # Balance check
    balance = _get_balance(db, employee_id, leave_type, year)
    if not balance:
        raise LeaveError(f"No leave balance found for {leave_type.value} in {year}")

    if leave_type != LeaveType.UNPAID:
        result = db.execute(
            update(LeaveBalance)
            .where(
                LeaveBalance.id == balance.id,
                LeaveBalance.total_days - LeaveBalance.used_days >= requested_days,
            )
            .values(used_days=LeaveBalance.used_days + requested_days)
        )
        if result.rowcount == 0:
            db.rollback()
            raise InsufficientBalanceError(
                f"Insufficient balance: {leave_type.value} has "
                f"{balance.remaining_days} days remaining, "
                f"but {requested_days} days were requested"
            )
    else:
        db.execute(
            update(LeaveBalance)
            .where(LeaveBalance.id == balance.id)
            .values(used_days=LeaveBalance.used_days + requested_days)
        )

    lr = LeaveRequest(
        employee_id=employee_id,
        leave_type=leave_type.value,
        start_date=start_date,
        end_date=end_date,
        duration=duration.value,
        reason=reason,
        status=LeaveStatus.PENDING.value,
    )
    db.add(lr)
    db.commit()
    db.refresh(lr)
    return lr


def review_leave_request(
    db: Session,
    leave_request_id: int,
    reviewer_id: int,
    decision: str,
    rejection_reason: str | None = None,
) -> LeaveRequest:
    if decision not in (LeaveStatus.APPROVED.value, LeaveStatus.REJECTED.value):
        raise LeaveError(
            f"Invalid decision: {decision}. Must be 'approved' or 'rejected'"
        )

    lr = db.query(LeaveRequest).filter(LeaveRequest.id == leave_request_id).first()
    if not lr:
        raise NotFoundError("Leave request not found")

    if lr.status != LeaveStatus.PENDING.value:
        raise AlreadyReviewedError("Leave request has already been reviewed")

    reviewer = db.query(Employee).filter(Employee.id == reviewer_id).first()
    if not reviewer:
        raise NotFoundError("Reviewer not found")

    requester = db.query(Employee).filter(Employee.id == lr.employee_id).first()

    # Self-review: only allowed for top-level employees (manager_id IS NULL)
    if reviewer_id == lr.employee_id:
        if reviewer.manager_id is not None:
            raise SelfReviewError("You cannot review your own leave request")
    else:
        # Reviewer must be the direct manager
        if requester.manager_id != reviewer_id:
            raise NotDirectManagerError(
                "Only the direct manager can review this leave request"
            )

    now = datetime.now(timezone.utc)

    # Conditional UPDATE — only succeeds if status is still 'pending'
    result = db.execute(
        update(LeaveRequest)
        .where(
            LeaveRequest.id == leave_request_id,
            LeaveRequest.status == LeaveStatus.PENDING.value,
        )
        .values(
            status=decision,
            reviewed_by=reviewer_id,
            reviewed_at=now,
            rejection_reason=rejection_reason,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        db.rollback()
        raise AlreadyReviewedError("Leave request was already reviewed concurrently")

    # Restore balance on rejection before committing — keeps both operations atomic
    if decision == LeaveStatus.REJECTED.value:
        working_days = count_working_days(
            db, lr.start_date, lr.end_date, LeaveDuration(lr.duration)
        )
        balance = _get_balance(
            db, lr.employee_id, LeaveType(lr.leave_type), lr.start_date.year
        )
        if balance:
            result = db.execute(
                update(LeaveBalance)
                .where(
                    LeaveBalance.id == balance.id,
                    LeaveBalance.used_days >= working_days,
                )
                .values(used_days=LeaveBalance.used_days - working_days)
            )
            if result.rowcount == 0:
                logger.warning(
                    "Balance restore failed for leave request %s: "
                    "used_days would go negative",
                    lr.id,
                )
        else:
            logger.warning(
                "Balance row missing during rejection of leave request "
                "%s: employee=%s type=%s year=%s",
                lr.id, lr.employee_id, lr.leave_type, lr.start_date.year,
            )

    db.commit()
    db.refresh(lr)
    return lr


def cancel_leave_request(
    db: Session,
    leave_request_id: int,
    employee_id: int,
) -> LeaveRequest:
    lr = db.query(LeaveRequest).filter(LeaveRequest.id == leave_request_id).first()
    if not lr:
        raise NotFoundError("Leave request not found")

    if lr.employee_id != employee_id:
        raise LeaveError("You can only cancel your own leave requests")

    if lr.status not in (LeaveStatus.PENDING.value, LeaveStatus.APPROVED.value):
        raise LeaveError("Only pending or approved leave requests can be cancelled")

    if lr.start_date < _today():
        raise LeaveError("Cannot cancel a leave request that has already started")

    now = datetime.now(timezone.utc)
    db.execute(
        update(LeaveRequest)
        .where(LeaveRequest.id == leave_request_id)
        .values(status=LeaveStatus.CANCELLED.value, updated_at=now)
    )

    # Restore balance
    working_days = count_working_days(
        db, lr.start_date, lr.end_date, LeaveDuration(lr.duration)
    )
    balance = _get_balance(
        db, lr.employee_id, LeaveType(lr.leave_type), lr.start_date.year
    )
    if balance:
        db.execute(
            update(LeaveBalance)
            .where(
                LeaveBalance.id == balance.id,
                LeaveBalance.used_days >= working_days,
            )
            .values(used_days=LeaveBalance.used_days - working_days)
        )
    else:
        logger.warning(
            "Balance row missing during cancellation of leave "
            "request %s: employee=%s type=%s year=%s",
            lr.id, lr.employee_id, lr.leave_type, lr.start_date.year,
        )

    db.commit()
    db.refresh(lr)
    return lr


def get_leave_request(
    db: Session,
    leave_request_id: int,
    caller_id: int,
) -> LeaveRequest:
    lr = db.query(LeaveRequest).filter(LeaveRequest.id == leave_request_id).first()
    if not lr:
        raise NotFoundError("Leave request not found")

    # Caller must be the owner or the owner's direct manager
    if lr.employee_id == caller_id:
        return lr

    owner = db.query(Employee).filter(Employee.id == lr.employee_id).first()
    if owner and owner.manager_id == caller_id:
        return lr

    raise NotFoundError("Leave request not found")


def get_leave_requests(
    db: Session,
    caller_id: int,
    employee_id: int | None = None,
    status: LeaveStatus | None = None,
    leave_type: LeaveType | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[LeaveRequest], int]:
    # Scope: caller's own requests + direct reports' requests
    direct_report_ids = [
        row[0]
        for row in db.query(Employee.id)
        .filter(Employee.manager_id == caller_id)
        .all()
    ]
    visible_ids = {caller_id} | set(direct_report_ids)

    query = db.query(LeaveRequest).filter(LeaveRequest.employee_id.in_(visible_ids))

    if employee_id is not None:
        if employee_id not in visible_ids:
            return [], 0
        query = query.filter(LeaveRequest.employee_id == employee_id)

    if status is not None:
        query = query.filter(LeaveRequest.status == status.value)

    if leave_type is not None:
        query = query.filter(LeaveRequest.leave_type == leave_type.value)

    if from_date is not None and to_date is not None:
        query = query.filter(
            LeaveRequest.start_date <= to_date,
            LeaveRequest.end_date >= from_date,
        )

    total = query.count()
    items = (
        query.order_by(LeaveRequest.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return items, total


def get_leave_balances(
    db: Session,
    employee_id: int,
    year: int | None = None,
) -> list[LeaveBalance]:
    if year is None:
        year = _today().year

    return (
        db.query(LeaveBalance)
        .filter(
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.year == year,
        )
        .all()
    )


# ── Employees ──────────────────────────────────────────────────────────────

def list_employees(
    db: Session,
    caller_id: int,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Employee], int]:
    query = db.query(Employee).filter(Employee.manager_id == caller_id)
    total = query.count()
    items = (
        query.order_by(Employee.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return items, total


def get_employee(
    db: Session,
    employee_id: int,
    caller_id: int,
) -> Employee | None:
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if employee is None:
        return None
    # Scope: caller can only see their own record or their direct reports
    if employee_id == caller_id:
        return employee
    if employee.manager_id == caller_id:
        return employee
    raise UnauthorizedAccessError("You do not have access to this employee's details")


# ── Holidays ───────────────────────────────────────────────────────────────

def list_holidays(
    db: Session,
    year: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[PublicHoliday], int]:
    query = db.query(PublicHoliday)
    if year is not None:
        query = query.filter(
            PublicHoliday.date >= date(year, 1, 1),
            PublicHoliday.date <= date(year, 12, 31),
        )
    total = query.count()
    items = (
        query.order_by(PublicHoliday.date)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return items, total


def create_holiday(
    db: Session,
    holiday_date: date,
    name: str,
    caller_id: int,
) -> PublicHoliday:
    caller = db.query(Employee).filter(Employee.id == caller_id).first()
    if not caller or caller.manager_id is not None:
        raise LeaveError("Only managers can manage holidays")

    existing = (
        db.query(PublicHoliday)
        .filter(PublicHoliday.date == holiday_date)
        .first()
    )
    if existing:
        raise LeaveError(f"A holiday already exists on {holiday_date}")

    holiday = PublicHoliday(date=holiday_date, name=name)
    db.add(holiday)
    db.commit()
    db.refresh(holiday)
    return holiday


def update_holiday(
    db: Session,
    holiday_id: int,
    holiday_date: date,
    name: str,
    caller_id: int,
) -> PublicHoliday:
    caller = db.query(Employee).filter(Employee.id == caller_id).first()
    if not caller or caller.manager_id is not None:
        raise LeaveError("Only managers can manage holidays")

    holiday = db.query(PublicHoliday).filter(PublicHoliday.id == holiday_id).first()
    if not holiday:
        raise NotFoundError("Holiday not found")

    dup = (
        db.query(PublicHoliday)
        .filter(PublicHoliday.date == holiday_date, PublicHoliday.id != holiday_id)
        .first()
    )
    if dup:
        raise LeaveError(f"A holiday already exists on {holiday_date}")

    holiday.date = holiday_date
    holiday.name = name
    db.commit()
    db.refresh(holiday)
    return holiday


def delete_holiday(db: Session, holiday_id: int, caller_id: int) -> None:
    caller = db.query(Employee).filter(Employee.id == caller_id).first()
    if not caller or caller.manager_id is not None:
        raise LeaveError("Only managers can manage holidays")

    holiday = db.query(PublicHoliday).filter(PublicHoliday.id == holiday_id).first()
    if not holiday:
        raise NotFoundError("Holiday not found")
    db.delete(holiday)
    db.commit()


# ── Seed Data ──────────────────────────────────────────────────────────────

def seed_demo_data(db: Session) -> None:
    """Seed demo employees, leave balances, and Malaysian public holidays."""
    existing = db.query(Employee).first()
    if existing:
        return

    # Employees — Alice is top-level (manager_id=NULL)
    alice = Employee(
        name="Alice Manager", email="alice@company.com", department="Engineering"
    )
    bob = Employee(
        name="Bob Engineer", email="bob@company.com",
        department="Engineering", manager=alice,
    )
    carol = Employee(
        name="Carol Engineer", email="carol@company.com",
        department="Engineering", manager=alice,
    )
    db.add_all([alice, bob, carol])
    db.flush()

    year = date.today().year
    leave_types = [
        LeaveType.ANNUAL,
        LeaveType.SICK,
        LeaveType.PERSONAL,
        LeaveType.MATERNITY,
        LeaveType.PATERNITY,
        LeaveType.UNPAID,
    ]

    for emp in [alice, bob, carol]:
        for lt in leave_types:
            total_days = (
                0.0 if lt == LeaveType.UNPAID
                else (14.0 if lt == LeaveType.ANNUAL else 12.0)
            )
            db.add(LeaveBalance(
                employee_id=emp.id,
                leave_type=lt.value,
                year=year,
                total_days=total_days,
                used_days=0.0,
            ))

    # Malaysian public holidays for 2026
    malaysian_holidays = [
        ("2026-01-01", "New Year's Day"),
        ("2026-02-17", "Chinese New Year"),
        ("2026-02-18", "Chinese New Year Holiday"),
        ("2026-03-20", "Hari Raya Puasa"),
        ("2026-03-21", "Hari Raya Puasa Holiday"),
        ("2026-05-01", "Labour Day"),
        ("2026-05-20", "Wesak Day"),
        ("2026-06-07", "Agong's Birthday"),
        ("2026-05-27", "Hari Raya Haji"),
        ("2026-08-31", "Merdeka Day"),
        ("2026-09-16", "Malaysia Day"),
        ("2026-10-31", "Deepavali"),
        ("2026-12-25", "Christmas Day"),
        ("2026-10-19", "Awal Muharram"),
    ]
    for holiday_date, name in malaysian_holidays:
        db.add(PublicHoliday(date=date.fromisoformat(holiday_date), name=name))

    db.commit()
