"""
Comprehensive pytest test suite for leave management services.
Covers all spec scenarios including concurrency correctness proof.
"""

import threading
import tempfile
import os
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import LeaveType, LeaveStatus, Employee, LeaveBalance, Holiday
from src import services


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def seeded_db(db):
    services.seed_demo_data(db)
    return db


@pytest.fixture
def alice(seeded_db):
    return seeded_db.query(Employee).filter(Employee.email == "alice@company.com").first()


@pytest.fixture
def bob(seeded_db):
    return seeded_db.query(Employee).filter(Employee.email == "bob@company.com").first()


@pytest.fixture
def carol(seeded_db):
    return seeded_db.query(Employee).filter(Employee.email == "carol@company.com").first()


@pytest.fixture
def david(seeded_db):
    return seeded_db.query(Employee).filter(Employee.email == "david@company.com").first()


# ── Test 1: Happy-path approval deducts exactly once ─────────────────────────


def test_happy_path_approval_deducts_exactly_once(seeded_db, alice, bob, monkeypatch):
    """Approve a 3-day request: balance deducted exactly once, one LeaveDeduction row."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    from src.models import LeaveDeduction

    # Ensure Bob has annual balance total=14, used=0 for 2026
    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        balance = LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        )
        seeded_db.add(balance)
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    # Submit 3-day request Mon 2026-06-01 to Wed 2026-06-03
    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
    )
    assert lr.status == LeaveStatus.PENDING

    # Alice approves
    approved = services.approve_leave_request(seeded_db, lr.id, alice.id, LeaveStatus.APPROVED)
    assert approved.status == LeaveStatus.APPROVED
    assert approved.approved_by == alice.id

    seeded_db.refresh(balance)
    assert balance.used_days == pytest.approx(3.0)

    deductions = seeded_db.query(LeaveDeduction).filter(
        LeaveDeduction.leave_request_id == lr.id
    ).all()
    assert len(deductions) == 1
    assert deductions[0].days == pytest.approx(3.0)


# ── Test 2: Insufficient balance at submission ────────────────────────────────


def test_insufficient_balance_at_submission(seeded_db, bob, monkeypatch):
    """Submit 5-day request with only 2 days total — InsufficientBalanceError."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    from src.models import LeaveDeduction, LeaveRequest

    # Set Bob's annual balance to total=2, used=0 for 2026
    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        balance = LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=2, used_days=0,
        )
        seeded_db.add(balance)
        seeded_db.commit()
    else:
        balance.total_days = 2
        balance.used_days = 0
        seeded_db.commit()

    with pytest.raises(services.InsufficientBalanceError):
        services.create_leave_request(
            seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 5)
        )

    # No LeaveRequest row created
    assert seeded_db.query(LeaveRequest).count() == 0
    # No LeaveDeduction row created
    assert seeded_db.query(LeaveDeduction).count() == 0


# ── Test 3: Own-leaves overlap ────────────────────────────────────────────────


def test_overlapping_leave_blocked(seeded_db, bob, monkeypatch):
    """Submit overlapping request — OverlappingLeaveError."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    from src.models import LeaveRequest

    # Give Bob enough balance
    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    # Existing pending request 2026-06-10 to 2026-06-12
    services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 10), date(2026, 6, 12)
    )

    # Overlapping new request 2026-06-11 to 2026-06-15
    with pytest.raises(services.OverlappingLeaveError):
        services.create_leave_request(
            seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 11), date(2026, 6, 15)
        )

    # Only one leave request exists
    assert seeded_db.query(LeaveRequest).count() == 1


def test_cancelled_rejected_do_not_block_overlap(seeded_db, alice, bob, monkeypatch):
    """Cancelled or rejected requests do NOT block overlapping submissions."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    from src.models import LeaveRequest

    # Give Bob enough balance for 2026
    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=30, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 30
        balance.used_days = 0
        seeded_db.commit()

    # Create and cancel a request for 2026-06-10 to 2026-06-12
    lr_cancelled = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 10), date(2026, 6, 12)
    )
    services.cancel_leave_request(seeded_db, lr_cancelled.id, bob.id)

    # Create and reject a request for 2026-06-15 to 2026-06-17
    lr_rejected = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 15), date(2026, 6, 17)
    )
    services.approve_leave_request(seeded_db, lr_rejected.id, alice.id, LeaveStatus.REJECTED)

    # Should succeed — overlaps with cancelled and rejected (which don't block)
    lr_new = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 10), date(2026, 6, 17)
    )
    assert lr_new is not None
    assert lr_new.status == LeaveStatus.PENDING


# ── Test 4: Backdated request ─────────────────────────────────────────────────


def test_backdated_request(seeded_db, bob, monkeypatch):
    """Submitting a request with start_date in the past raises BackdatedRequestError."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 6, 5))
    with pytest.raises(services.BackdatedRequestError):
        services.create_leave_request(
            seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
        )


# ── Test 5: Half-day deduction ────────────────────────────────────────────────


def test_half_day_deduction(seeded_db, bob, monkeypatch):
    """Half-day request: _estimated_deductions=[{year:2026, days:0.5}]."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL,
        date(2026, 6, 1), date(2026, 6, 1),
        half_day_start=True,
    )
    assert lr._estimated_deductions == [{"year": 2026, "days": 0.5}]


# ── Test 6: Weekend exclusion ─────────────────────────────────────────────────


def test_weekend_exclusion(seeded_db, bob, monkeypatch):
    """Fri to Mon: Sat+Sun excluded, so 2.0 working days deducted."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    # Fri 2026-06-05 to Mon 2026-06-08 → 2 working days (Fri, Mon)
    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 5), date(2026, 6, 8)
    )
    assert lr._estimated_deductions == [{"year": 2026, "days": 2.0}]


# ── Test 7: Public holiday exclusion ─────────────────────────────────────────


def test_public_holiday_exclusion(seeded_db, bob, monkeypatch):
    """Public holiday excluded from deduction count."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    # Insert a public holiday (seed_demo_data may have already inserted this date)
    if not seeded_db.query(Holiday).filter(Holiday.date == date(2026, 8, 12)).first():
        seeded_db.add(Holiday(date=date(2026, 8, 12), name="Test Holiday"))
        seeded_db.commit()

    # Mon 2026-08-10 to Fri 2026-08-14 = 5 working days - 1 holiday = 4.0
    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 8, 10), date(2026, 8, 14)
    )
    assert lr._estimated_deductions == [{"year": 2026, "days": 4.0}]


# ── Test 8: Year-boundary split ───────────────────────────────────────────────


def test_year_boundary_split(seeded_db, bob, monkeypatch):
    """Year-spanning request splits deductions across two years."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    # Create explicit 2026 and 2027 annual balances
    bal_2026 = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if bal_2026 is None:
        bal_2026 = LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=9,
        )
        seeded_db.add(bal_2026)
    else:
        bal_2026.total_days = 14
        bal_2026.used_days = 9

    bal_2027 = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2027,
    ).first()
    if bal_2027 is None:
        bal_2027 = LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2027, total_days=14, used_days=0,
        )
        seeded_db.add(bal_2027)
    else:
        bal_2027.total_days = 14
        bal_2027.used_days = 0

    # Add holiday on 2027-01-01 (seed_demo_data may have already inserted this date)
    if not seeded_db.query(Holiday).filter(Holiday.date == date(2027, 1, 1)).first():
        seeded_db.add(Holiday(date=date(2027, 1, 1), name="New Year"))
    seeded_db.commit()

    # 2026-12-29 (Tue) to 2027-01-05 (Tue)
    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 12, 29), date(2027, 1, 5)
    )

    deductions = {d["year"]: d["days"] for d in lr._estimated_deductions}
    assert deductions[2026] == pytest.approx(3.0)  # Dec 29, 30, 31 (Tue, Wed, Thu)
    assert 2027 in deductions
    assert deductions[2027] > 0


# ── Test 9: Self-approval blocked ────────────────────────────────────────────


def test_self_approval_blocked(seeded_db, bob, monkeypatch):
    """Employee cannot approve their own leave request."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
    )

    with pytest.raises(services.SelfApprovalError):
        services.approve_leave_request(seeded_db, lr.id, bob.id, LeaveStatus.APPROVED)

    seeded_db.refresh(lr)
    assert lr.status == LeaveStatus.PENDING

    seeded_db.refresh(balance)
    assert balance.used_days == pytest.approx(0.0)


# ── Test 10: Re-approval of already-approved ─────────────────────────────────


def test_re_approval_raises(seeded_db, alice, bob, monkeypatch):
    """Approving an already-approved request raises RequestNotPendingError."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    from src.models import LeaveDeduction

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
    )
    services.approve_leave_request(seeded_db, lr.id, alice.id, LeaveStatus.APPROVED)

    with pytest.raises(services.RequestNotPendingError):
        services.approve_leave_request(seeded_db, lr.id, alice.id, LeaveStatus.APPROVED)

    seeded_db.refresh(balance)
    assert balance.used_days == pytest.approx(3.0)

    deduction_count = seeded_db.query(LeaveDeduction).filter(
        LeaveDeduction.leave_request_id == lr.id
    ).count()
    assert deduction_count == 1


# ── Test 11: Concurrent approval — THE HEADLINE TEST ─────────────────────────


def test_concurrent_approval_exactly_once(monkeypatch):
    """Two simultaneous approvals on the same request — exactly one wins."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from src.database import Base

        db_url = f"sqlite:///{db_path}"
        eng = create_engine(db_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=eng)
        SessionFactory = sessionmaker(bind=eng)

        # Seed data in a separate session
        setup_db = SessionFactory()
        services.seed_demo_data(setup_db)
        alice = setup_db.query(Employee).filter(Employee.email == "alice@company.com").first()
        bob = setup_db.query(Employee).filter(Employee.email == "bob@company.com").first()

        # Give Bob 14 annual days for 2026
        balance = setup_db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == bob.id,
            LeaveBalance.leave_type == LeaveType.ANNUAL,
        ).first()
        if balance is None:
            balance = LeaveBalance(
                employee_id=bob.id, leave_type=LeaveType.ANNUAL,
                year=2026, total_days=14, used_days=0,
            )
            setup_db.add(balance)
            setup_db.commit()
        else:
            balance.total_days = 14
            balance.used_days = 0
            setup_db.commit()

        # Submit a 3-day leave request
        monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))
        lr = services.create_leave_request(
            setup_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
        )
        lr_id = lr.id
        alice_id = alice.id
        bob_id = bob.id
        setup_db.close()

        # Two threads, each with their own session, both try to approve
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def try_approve():
            session = SessionFactory()
            try:
                barrier.wait()  # synchronize both threads to hit CAS at the same time
                result = services.approve_leave_request(session, lr_id, alice_id, LeaveStatus.APPROVED)
                results.append(result)
            except services.RequestNotPendingError as e:
                errors.append(e)
            except Exception as e:
                errors.append(e)
            finally:
                session.close()

        t1 = threading.Thread(target=try_approve)
        t2 = threading.Thread(target=try_approve)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one winner, one loser
        assert len(results) == 1, f"Expected 1 success, got {len(results)}: {results}"
        assert len(errors) == 1, f"Expected 1 error, got {len(errors)}: {errors}"
        assert isinstance(errors[0], services.RequestNotPendingError)

        # Balance must be exactly 3.0 used (not 6.0 or 0.0)
        verify_db = SessionFactory()
        final_balance = verify_db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == bob_id,
            LeaveBalance.leave_type == LeaveType.ANNUAL,
        ).first()
        assert final_balance.used_days == pytest.approx(3.0), \
            f"used_days={final_balance.used_days}, expected 3.0"

        # Exactly one deduction row
        from src.models import LeaveDeduction
        deduction_count = verify_db.query(LeaveDeduction).filter(
            LeaveDeduction.leave_request_id == lr_id
        ).count()
        assert deduction_count == 1
        verify_db.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# ── Test 12: Double-click idempotency ─────────────────────────────────────────


def test_double_click_idempotency(seeded_db, alice, bob, monkeypatch):
    """Sequential double-approval: first succeeds, second raises RequestNotPendingError."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    from src.models import LeaveDeduction

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
    )

    # First approval succeeds
    services.approve_leave_request(seeded_db, lr.id, alice.id, LeaveStatus.APPROVED)

    # Second approval raises
    with pytest.raises(services.RequestNotPendingError):
        services.approve_leave_request(seeded_db, lr.id, alice.id, LeaveStatus.APPROVED)

    seeded_db.refresh(balance)
    assert balance.used_days == pytest.approx(3.0)

    deduction_count = seeded_db.query(LeaveDeduction).filter(
        LeaveDeduction.leave_request_id == lr.id
    ).count()
    assert deduction_count == 1


# ── Test 13: Balance changed between submission and approval ──────────────────


def test_balance_changed_between_submission_and_approval(seeded_db, alice, bob, monkeypatch):
    """If balance drops below required before approval, InsufficientBalanceError."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    from src.models import LeaveDeduction

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        balance = LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=5, used_days=0,
        )
        seeded_db.add(balance)
        seeded_db.commit()
    else:
        balance.total_days = 5
        balance.used_days = 0
        seeded_db.commit()

    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 5)
    )

    # Manually consume 3 days, leaving only 2 remaining (request needs 5)
    seeded_db.refresh(balance)
    balance.used_days = 3.0
    seeded_db.commit()

    with pytest.raises(services.InsufficientBalanceError):
        services.approve_leave_request(seeded_db, lr.id, alice.id, LeaveStatus.APPROVED)

    seeded_db.refresh(lr)
    assert lr.status == LeaveStatus.PENDING

    deduction_count = seeded_db.query(LeaveDeduction).filter(
        LeaveDeduction.leave_request_id == lr.id
    ).count()
    assert deduction_count == 0


# ── Test 14: Cancel pending request ──────────────────────────────────────────


def test_cancel_pending_request(seeded_db, bob, monkeypatch):
    """Cancelling a pending request sets status=cancelled, balance unchanged."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
    )

    cancelled = services.cancel_leave_request(seeded_db, lr.id, bob.id)
    assert cancelled.status == LeaveStatus.CANCELLED

    seeded_db.refresh(balance)
    assert balance.used_days == pytest.approx(0.0)
    assert cancelled._restored_deductions == []


# ── Test 15: Cancel approved request restores balance ─────────────────────────


def test_cancel_approved_restores_balance(seeded_db, alice, bob, monkeypatch):
    """Cancelling an approved 3-day request restores balance by 3.0."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    from src.models import LeaveDeduction

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        balance = LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        )
        seeded_db.add(balance)
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
    )
    services.approve_leave_request(seeded_db, lr.id, alice.id, LeaveStatus.APPROVED)

    seeded_db.refresh(balance)
    assert balance.used_days == pytest.approx(3.0)

    cancelled = services.cancel_leave_request(seeded_db, lr.id, bob.id)
    assert cancelled.status == LeaveStatus.CANCELLED

    restored = cancelled._restored_deductions
    assert len(restored) == 1
    assert restored[0]["days"] == pytest.approx(3.0)

    seeded_db.refresh(balance)
    assert balance.used_days == pytest.approx(0.0)

    deduction_count = seeded_db.query(LeaveDeduction).filter(
        LeaveDeduction.leave_request_id == lr.id
    ).count()
    assert deduction_count == 0


# ── Test 16: Cancel year-spanning request restores both years ─────────────────


def test_cancel_year_spanning_restores_both_years(seeded_db, alice, bob, monkeypatch):
    """Cancelling a year-spanning approved request restores both year balances."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    from src.models import LeaveDeduction

    bal_2026 = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if bal_2026 is None:
        bal_2026 = LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        )
        seeded_db.add(bal_2026)
    else:
        bal_2026.total_days = 14
        bal_2026.used_days = 0

    bal_2027 = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2027,
    ).first()
    if bal_2027 is None:
        bal_2027 = LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2027, total_days=14, used_days=0,
        )
        seeded_db.add(bal_2027)
    else:
        bal_2027.total_days = 14
        bal_2027.used_days = 0

    seeded_db.commit()

    # 2026-12-29 to 2027-01-02: 3 days in 2026 (Tue, Wed, Thu), 2 in 2027 (Fri, Jan2 Mon... wait)
    # 2026-12-29=Tue, 30=Wed, 31=Thu → 3 working days in 2026
    # 2027-01-01=Fri (holiday or not?), 2027-01-02=Sat, 2027-01-04=Mon... but end is 01-02
    # Let's use 2026-12-29 to 2027-01-04: 3 days 2026, at least 1-2 in 2027
    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 12, 29), date(2027, 1, 4)
    )
    lr_id = lr.id
    deductions_at_creation = {d["year"]: d["days"] for d in lr._estimated_deductions}
    days_2026 = deductions_at_creation[2026]
    days_2027 = deductions_at_creation.get(2027, 0)

    services.approve_leave_request(seeded_db, lr_id, alice.id, LeaveStatus.APPROVED)

    seeded_db.refresh(bal_2026)
    seeded_db.refresh(bal_2027)
    assert bal_2026.used_days == pytest.approx(days_2026)
    assert bal_2027.used_days == pytest.approx(days_2027)

    deduction_count = seeded_db.query(LeaveDeduction).filter(
        LeaveDeduction.leave_request_id == lr_id
    ).count()
    assert deduction_count >= 1  # at least 2026 deduction

    cancelled = services.cancel_leave_request(seeded_db, lr_id, bob.id)
    assert cancelled.status == LeaveStatus.CANCELLED

    seeded_db.refresh(bal_2026)
    seeded_db.refresh(bal_2027)
    assert bal_2026.used_days == pytest.approx(0.0)
    assert bal_2027.used_days == pytest.approx(0.0)

    deduction_count = seeded_db.query(LeaveDeduction).filter(
        LeaveDeduction.leave_request_id == lr_id
    ).count()
    assert deduction_count == 0


# ── Test 17: Non-owner cannot cancel ─────────────────────────────────────────


def test_non_owner_cannot_cancel(seeded_db, bob, carol, monkeypatch):
    """Bob cannot cancel Carol's leave request."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    carol_balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == carol.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if carol_balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=carol.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        carol_balance.total_days = 14
        carol_balance.used_days = 0
        seeded_db.commit()

    lr = services.create_leave_request(
        seeded_db, carol.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
    )

    with pytest.raises(services.NotRequestOwnerError):
        services.cancel_leave_request(seeded_db, lr.id, bob.id)

    seeded_db.refresh(lr)
    assert lr.status == LeaveStatus.PENDING


# ── Test 18: Second cancel is a no-op ────────────────────────────────────────


def test_second_cancel_raises(seeded_db, bob, monkeypatch):
    """Cancelling an already-cancelled request raises AlreadyCancelledError."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
    )
    services.cancel_leave_request(seeded_db, lr.id, bob.id)

    with pytest.raises(services.AlreadyCancelledError):
        services.cancel_leave_request(seeded_db, lr.id, bob.id)

    seeded_db.refresh(balance)
    assert balance.used_days == pytest.approx(0.0)


# ── Test 19: Rejected request cannot be cancelled ────────────────────────────


def test_rejected_cannot_be_cancelled(seeded_db, alice, bob, monkeypatch):
    """Cancelling a rejected request raises RejectedRequestNotCancellableError."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    balance = seeded_db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == bob.id,
        LeaveBalance.leave_type == LeaveType.ANNUAL,
        LeaveBalance.year == 2026,
    ).first()
    if balance is None:
        seeded_db.add(LeaveBalance(
            employee_id=bob.id, leave_type=LeaveType.ANNUAL,
            year=2026, total_days=14, used_days=0,
        ))
        seeded_db.commit()
    else:
        balance.total_days = 14
        balance.used_days = 0
        seeded_db.commit()

    lr = services.create_leave_request(
        seeded_db, bob.id, LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 3)
    )
    services.approve_leave_request(seeded_db, lr.id, alice.id, LeaveStatus.REJECTED)

    with pytest.raises(services.RejectedRequestNotCancellableError):
        services.cancel_leave_request(seeded_db, lr.id, bob.id)


# ── Test 20: Missing balance row returns zero, not error ──────────────────────


def test_missing_balance_returns_zero(seeded_db, bob):
    """get_leave_balances returns zero-synthesized row for paternity leave."""
    balances = services.get_leave_balances(seeded_db, bob.id, year=2026)
    paternity_rows = [b for b in balances if b.leave_type == LeaveType.PATERNITY]
    assert len(paternity_rows) == 1
    assert paternity_rows[0].total_days == 0
    assert paternity_rows[0].used_days == 0
    assert paternity_rows[0].remaining_days == 0


# ── Test 21: List filtering and pagination ────────────────────────────────────


def test_list_filtering_and_pagination(seeded_db, alice, bob, carol, monkeypatch):
    """get_leave_requests: total is full matching count, items is paged slice."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 5, 1))

    from src.models import LeaveRequest

    # Give bob and carol enough balance for 2026
    for emp in [bob, carol]:
        balance = seeded_db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == emp.id,
            LeaveBalance.leave_type == LeaveType.ANNUAL,
            LeaveBalance.year == 2026,
        ).first()
        if balance is None:
            seeded_db.add(LeaveBalance(
                employee_id=emp.id, leave_type=LeaveType.ANNUAL,
                year=2026, total_days=100, used_days=0,
            ))
        else:
            balance.total_days = 100
            balance.used_days = 0
        seeded_db.commit()

    # Insert 35 leave requests: mix of approved/pending, annual/sick
    # Approved annual requests for bob: 25 (spread over months)
    for i in range(25):
        start = date(2026, 6, 1 + i)
        # skip weekends by checking
        # just use a monday offset in August so we have plenty of room
        start = date(2026, 8, 1 + i)
        lr = seeded_db.query(LeaveRequest).filter(
            LeaveRequest.employee_id == bob.id,
            LeaveRequest.start_date == start,
        ).first()
        if lr is None:
            lr = LeaveRequest(
                employee_id=bob.id,
                leave_type=LeaveType.ANNUAL,
                start_date=start,
                end_date=start,
                status=LeaveStatus.APPROVED,
                approved_by=alice.id,
            )
            seeded_db.add(lr)

    # Pending annual requests for carol: 10
    for i in range(10):
        start = date(2026, 9, 1 + i)
        lr = seeded_db.query(LeaveRequest).filter(
            LeaveRequest.employee_id == carol.id,
            LeaveRequest.start_date == start,
        ).first()
        if lr is None:
            lr = LeaveRequest(
                employee_id=carol.id,
                leave_type=LeaveType.ANNUAL,
                start_date=start,
                end_date=start,
                status=LeaveStatus.PENDING,
            )
            seeded_db.add(lr)

    seeded_db.commit()

    # Query: approved annual, page 2, page_size 10
    items, total = services.get_leave_requests(
        seeded_db,
        status=LeaveStatus.APPROVED,
        leave_type=LeaveType.ANNUAL,
        page=2,
        page_size=10,
    )

    # Total should be count of all matching rows (25 approved annual for bob)
    assert total == 25
    # Page 2 of 10 → items 11-20
    assert len(items) == 10


# ── Test 22: Scenario Outline — invalid submissions (parametrized) ────────────


@pytest.mark.parametrize("kwargs,exc_class", [
    ({"start_date": date(2026, 5, 1), "end_date": date(2026, 5, 31)}, services.BackdatedRequestError),
    ({"start_date": date(2026, 7, 10), "end_date": date(2026, 7, 5)}, services.InvalidDateRangeError),
    # all-weekend range
    ({"start_date": date(2026, 6, 6), "end_date": date(2026, 6, 7)}, services.NoWorkingDaysError),
])
def test_invalid_submissions(seeded_db, bob, monkeypatch, kwargs, exc_class):
    """Parametrized test: each invalid scenario raises the correct exception."""
    monkeypatch.setattr(services, "_today", lambda: date(2026, 6, 1))
    base_kwargs = dict(
        db=seeded_db,
        employee_id=bob.id,
        leave_type=LeaveType.ANNUAL,
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 3),
    )
    base_kwargs.update(kwargs)
    with pytest.raises(exc_class):
        services.create_leave_request(**base_kwargs)
