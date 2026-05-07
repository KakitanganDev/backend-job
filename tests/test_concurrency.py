"""
Concurrency proof.

These tests confirm that the pessimistic lock + transaction discipline
in services.py actually serialises competing writers. They run real
threads against a real SQLite file, each with its own Session, hitting
the service layer simultaneously through a barrier.

Marked `slow` because barriers + sleeps make them an order of magnitude
slower than the unit tests. CI runs them; local `pytest -m 'not slow'`
can skip.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta

import pytest

from src import services
from src.models import LeaveBalance, LeaveStatus, LeaveType

pytestmark = pytest.mark.slow


def _next_monday() -> date:
    d = date.today()
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


def _run_in_threads(targets, n_threads):
    """Run `targets[i]` in thread i. Targets get a shared `barrier` arg."""
    results = [None] * n_threads
    errors = [None] * n_threads
    barrier = threading.Barrier(n_threads)

    def wrap(i):
        try:
            results[i] = targets[i](barrier)
        except Exception as e:
            errors[i] = e

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_two_managers_race_only_one_wins(seed):
    """Two threads call approve_leave_request on the same pending leave.

    Exactly one must succeed; the other must raise
    CannotModifyApprovedLeaveError. Balance must be deducted exactly once.
    """
    from src.database import SessionLocal

    alice, bob = seed["alice"], seed["bob"]
    start = _next_monday()

    # Setup: create a request in a separate session so both threads see it.
    with SessionLocal() as setup_db:
        lr = services.create_leave_request(
            setup_db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            start_date=start, end_date=start + timedelta(days=2),
        )
        lr_id = lr.id
        bob_id = bob.id
        alice_id = alice.id

    def approve(barrier):
        db = SessionLocal()
        try:
            barrier.wait()
            return services.approve_leave_request(
                db, leave_request_id=lr_id,
                approver_id=alice_id, decision=LeaveStatus.APPROVED,
            )
        finally:
            db.close()

    results, errors = _run_in_threads([approve, approve], 2)

    successes = [r for r in results if r is not None]
    failures = [e for e in errors if e is not None]
    assert len(successes) == 1, f"Expected exactly 1 success; got results={results} errors={errors}"
    assert len(failures) == 1
    assert isinstance(failures[0], services.CannotModifyApprovedLeaveError)

    with SessionLocal() as check_db:
        bal = check_db.query(LeaveBalance).filter_by(
            employee_id=bob_id, leave_type=LeaveType.ANNUAL,
        ).one()
        assert bal.used_days == 3.0, f"balance double-deducted: used_days={bal.used_days}"


def test_concurrent_approvals_respect_balance_ceiling(seed):
    """Two pending leaves whose combined cost exceeds the balance.

    Approving both concurrently must result in:
      - exactly one approval,
      - the other raising InsufficientBalanceError,
      - balance not negative.
    """
    from src.database import SessionLocal

    alice, bob = seed["alice"], seed["bob"]

    # Squeeze the balance: 1 day. Request two 1-day leaves on different
    # plain weekdays. Sum is 2 > 1, so exactly one approval should win
    # and the other must hit InsufficientBalanceError.
    with SessionLocal() as db:
        bal = db.query(LeaveBalance).filter_by(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
        ).one()
        bal.total_days = 1
        db.commit()

        # Pick two clearly-weekday non-holiday days: Tue & Thu of next
        # plain workweek (avoids running into MY holidays in May/June).
        mon = _next_monday()
        day_a = mon + timedelta(days=1)
        day_b = mon + timedelta(days=3)
        lr_a = services.create_leave_request(
            db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            start_date=day_a, end_date=day_a,
        )
        lr_b = services.create_leave_request(
            db, employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            start_date=day_b, end_date=day_b,
        )
        ids = (lr_a.id, lr_b.id)
        bob_id = bob.id
        alice_id = alice.id

    def make_approver(lr_id):
        def _approve(barrier):
            db = SessionLocal()
            try:
                barrier.wait()
                return services.approve_leave_request(
                    db, leave_request_id=lr_id,
                    approver_id=alice_id, decision=LeaveStatus.APPROVED,
                )
            finally:
                db.close()
        return _approve

    results, errors = _run_in_threads(
        [make_approver(ids[0]), make_approver(ids[1])], 2,
    )

    successes = [r for r in results if r is not None]
    insufficient = [
        e for e in errors
        if isinstance(e, services.InsufficientBalanceError)
    ]
    assert len(successes) == 1
    assert len(insufficient) == 1

    with SessionLocal() as check_db:
        bal = check_db.query(LeaveBalance).filter_by(
            employee_id=bob_id, leave_type=LeaveType.ANNUAL,
        ).one()
        assert bal.used_days == 1.0  # only the winner's 1-day deduction
        assert bal.remaining_days == 0.0
