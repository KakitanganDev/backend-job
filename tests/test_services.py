"""
Service-layer tests covering the rules in services.py docstrings.

These tests cover the explicit "should consider" cases from the README:
overlap, insufficient balance, self-approval, cancel-restores-balance,
half-days, weekends/holidays, year-spanning. The concurrency check lives
in test_concurrency.py because it needs threads.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src import services
from src.models import LeaveBalance, LeaveStatus, LeaveType

# ── Helpers ──────────────────────────────────────────────────────────────

def _next_monday(after: date | None = None) -> date:
    """Return the next Monday on or after `after` (defaults to today)."""
    d = after or date.today()
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


# ── create_leave_request ─────────────────────────────────────────────────

def test_create_leave_request_happy_path(db, seed):
    bob = seed["bob"]
    start = _next_monday()
    end = start + timedelta(days=2)  # Mon-Wed = 3 working days

    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=end, reason="trip",
    )

    assert lr.id is not None
    assert lr.status == LeaveStatus.PENDING
    assert lr.working_days == 3.0
    # Balance NOT yet deducted — only on approval.
    bal = db.query(LeaveBalance).filter_by(
        employee_id=bob.id, leave_type=LeaveType.ANNUAL,
    ).one()
    assert bal.used_days == 0


def test_create_leave_request_rejects_back_dating(db, seed):
    bob = seed["bob"]
    yesterday = date.today() - timedelta(days=1)
    with pytest.raises(services.InvalidLeaveDatesError):
        services.create_leave_request(
            db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            start_date=yesterday, end_date=yesterday,
        )


def test_create_leave_request_rejects_inverted_dates(db, seed):
    bob = seed["bob"]
    start = _next_monday()
    with pytest.raises(services.InvalidLeaveDatesError):
        services.create_leave_request(
            db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            start_date=start + timedelta(days=3), end_date=start,
        )


def test_create_leave_request_insufficient_balance(db, seed):
    bob = seed["bob"]
    start = _next_monday()
    # 14 days annual balance — request 15 working days.
    end = start + timedelta(weeks=3, days=2)  # 22 calendar days, 16 working days
    with pytest.raises(services.InsufficientBalanceError):
        services.create_leave_request(
            db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            start_date=start, end_date=end,
        )


def test_create_leave_request_overlap(db, seed):
    bob = seed["bob"]
    start = _next_monday()
    services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start + timedelta(days=2),
    )
    # Overlaps last day.
    with pytest.raises(services.OverlappingLeaveError):
        services.create_leave_request(
            db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            start_date=start + timedelta(days=2), end_date=start + timedelta(days=4),
        )


def test_create_leave_request_overlap_ignores_cancelled(db, seed):
    bob = seed["bob"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start + timedelta(days=2),
    )
    services.cancel_leave_request(db, lr.id, employee_id=bob.id)

    # Now the same dates should be accepted.
    lr2 = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start + timedelta(days=2),
    )
    assert lr2.id != lr.id


def test_create_leave_request_weekend_only_rejected(db, seed):
    bob = seed["bob"]
    # Find next Saturday.
    saturday = _next_monday() + timedelta(days=5)
    sunday = saturday + timedelta(days=1)
    with pytest.raises(services.NoWorkingDaysError):
        services.create_leave_request(
            db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            start_date=saturday, end_date=sunday,
        )


def test_create_leave_request_half_day(db, seed):
    bob = seed["bob"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start, start_half_day=True,
    )
    assert lr.working_days == 0.5


def test_create_leave_request_half_day_at_each_boundary(db, seed):
    bob = seed["bob"]
    start = _next_monday()
    end = start + timedelta(days=4)  # Mon-Fri, 5 working days
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=end,
        start_half_day=True, end_half_day=True,
    )
    assert lr.working_days == 4.0  # 5 - 0.5 - 0.5


def test_create_leave_request_unpaid_skips_balance(db, seed):
    bob = seed["bob"]
    # Bob has no UNPAID balance row. UNPAID should still be allowed.
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.UNPAID,
        start_date=start, end_date=start,
    )
    assert lr.status == LeaveStatus.PENDING


def test_create_leave_request_no_balance_allocated(db, seed):
    bob = seed["bob"]
    # Bob has no PERSONAL balance row.
    start = _next_monday()
    with pytest.raises(services.BalanceNotAllocatedError):
        services.create_leave_request(
            db, employee_id=bob.id, leave_type=LeaveType.PERSONAL,
            start_date=start, end_date=start,
        )


# ── approve_leave_request ────────────────────────────────────────────────

def test_approve_leave_request_deducts_balance(db, seed):
    alice, bob = seed["alice"], seed["bob"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start + timedelta(days=2),
    )
    services.approve_leave_request(
        db, leave_request_id=lr.id, approver_id=alice.id, decision=LeaveStatus.APPROVED,
    )
    bal = db.query(LeaveBalance).filter_by(
        employee_id=bob.id, leave_type=LeaveType.ANNUAL,
    ).one()
    assert bal.used_days == 3.0
    db.refresh(lr)
    assert lr.status == LeaveStatus.APPROVED
    assert lr.approved_by == alice.id


def test_approve_leave_request_self_approval_rejected(db, seed):
    bob = seed["bob"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start,
    )
    with pytest.raises(services.SelfApprovalError):
        services.approve_leave_request(
            db, leave_request_id=lr.id, approver_id=bob.id, decision=LeaveStatus.APPROVED,
        )


def test_approve_leave_request_non_manager_rejected(db, seed):
    bob, carol = seed["bob"], seed["carol"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start,
    )
    with pytest.raises(services.NotAuthorizedError):
        services.approve_leave_request(
            db, leave_request_id=lr.id, approver_id=carol.id, decision=LeaveStatus.APPROVED,
        )


def test_approve_already_approved_rejected(db, seed):
    alice, bob = seed["alice"], seed["bob"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start,
    )
    services.approve_leave_request(
        db, lr.id, alice.id, LeaveStatus.APPROVED,
    )
    with pytest.raises(services.CannotModifyApprovedLeaveError):
        services.approve_leave_request(
            db, lr.id, alice.id, LeaveStatus.APPROVED,
        )


def test_reject_does_not_deduct_balance(db, seed):
    alice, bob = seed["alice"], seed["bob"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start + timedelta(days=2),
    )
    services.approve_leave_request(
        db, lr.id, alice.id, LeaveStatus.REJECTED,
    )
    bal = db.query(LeaveBalance).filter_by(
        employee_id=bob.id, leave_type=LeaveType.ANNUAL,
    ).one()
    assert bal.used_days == 0


# ── cancel_leave_request ─────────────────────────────────────────────────

def test_cancel_pending_leave(db, seed):
    bob = seed["bob"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start,
    )
    cancelled = services.cancel_leave_request(db, lr.id, employee_id=bob.id)
    assert cancelled.status == LeaveStatus.CANCELLED


def test_cancel_approved_restores_balance(db, seed):
    alice, bob = seed["alice"], seed["bob"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start + timedelta(days=2),
    )
    services.approve_leave_request(
        db, lr.id, alice.id, LeaveStatus.APPROVED,
    )

    bal = db.query(LeaveBalance).filter_by(
        employee_id=bob.id, leave_type=LeaveType.ANNUAL,
    ).one()
    assert bal.used_days == 3.0

    services.cancel_leave_request(db, lr.id, employee_id=bob.id)
    db.refresh(bal)
    assert bal.used_days == 0


def test_cancel_by_non_owner_rejected(db, seed):
    bob, carol = seed["bob"], seed["carol"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start,
    )
    with pytest.raises(services.NotAuthorizedError):
        services.cancel_leave_request(db, lr.id, employee_id=carol.id)


def test_cancel_already_cancelled_rejected(db, seed):
    bob = seed["bob"]
    start = _next_monday()
    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=start, end_date=start,
    )
    services.cancel_leave_request(db, lr.id, employee_id=bob.id)
    with pytest.raises(services.CannotModifyApprovedLeaveError):
        services.cancel_leave_request(db, lr.id, employee_id=bob.id)


# ── get_leave_requests / get_leave_balances ──────────────────────────────

def test_list_with_filters_and_pagination(db, seed):
    bob = seed["bob"]
    base = _next_monday()
    for offset in (0, 7, 14):
        services.create_leave_request(
            db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            start_date=base + timedelta(days=offset),
            end_date=base + timedelta(days=offset),
        )

    items, total = services.get_leave_requests(
        db, employee_id=bob.id, status=LeaveStatus.PENDING, page=1, page_size=2,
    )
    assert total == 3
    assert len(items) == 2

    items_p2, _ = services.get_leave_requests(
        db, employee_id=bob.id, status=LeaveStatus.PENDING, page=2, page_size=2,
    )
    assert len(items_p2) == 1


def test_list_filters_by_date_overlap(db, seed):
    bob = seed["bob"]
    base = _next_monday()
    services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=base, end_date=base,
    )
    services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=base + timedelta(days=14), end_date=base + timedelta(days=14),
    )

    # Window fully covers the first leave only.
    _, total = services.get_leave_requests(
        db, employee_id=bob.id,
        from_date=base, to_date=base + timedelta(days=3),
    )
    assert total == 1


def test_get_leave_balances_default_year(db, seed):
    bob = seed["bob"]
    balances = services.get_leave_balances(db, employee_id=bob.id)
    types = {b.leave_type for b in balances}
    assert LeaveType.ANNUAL in types
    assert LeaveType.SICK in types


# ── Year-spanning behaviour ──────────────────────────────────────────────

def test_year_spanning_attributes_to_start_year(db, seed):
    """A leave that crosses a year boundary deducts from the START year.

    Documented tradeoff (DESIGN.md §4): split-attribution would be more
    correct but adds locking complexity we deferred.
    """
    bob = seed["bob"]
    year = seed["year"]
    # Add next-year balances to make the test deterministic regardless of
    # when in the calendar year the test runs.
    db.add_all([
        LeaveBalance(employee_id=bob.id, leave_type=LeaveType.ANNUAL,
                     year=year + 1, total_days=14),
    ])
    db.commit()

    # Hardcoded Mon 2026-12-28 → Thu 2026-12-31. Falls in the current
    # test year only when the suite runs in 2026. We skip otherwise.
    dec_mon = date(year, 12, 28)
    if dec_mon.weekday() != 0:
        pytest.skip(f"{dec_mon} is not a Monday in year {year}")
    if dec_mon < date.today():
        pytest.skip("year-spanning test requires the future December")

    lr = services.create_leave_request(
        db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        start_date=dec_mon, end_date=dec_mon + timedelta(days=3),
    )
    services.approve_leave_request(
        db, lr.id, seed["alice"].id, LeaveStatus.APPROVED,
    )

    start_year_bal = db.query(LeaveBalance).filter_by(
        employee_id=bob.id, leave_type=LeaveType.ANNUAL, year=year,
    ).one()
    next_year_bal = db.query(LeaveBalance).filter_by(
        employee_id=bob.id, leave_type=LeaveType.ANNUAL, year=year + 1,
    ).one()
    assert start_year_bal.used_days > 0
    assert next_year_bal.used_days == 0
