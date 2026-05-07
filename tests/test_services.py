"""
Tests for the leave management service layer.

All tests use in-memory SQLite with fresh seed data per test.
"""

import unittest
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, update as sa_update
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import Employee, LeaveRequest, LeaveBalance, PublicHoliday, LeaveType, LeaveStatus, LeaveDuration
from src.services import (
    seed_demo_data,
    create_leave_request,
    review_leave_request,
    cancel_leave_request,
    get_leave_request,
    get_leave_requests,
    get_leave_balances,
    list_employees,
    get_employee,
    count_working_days,
    list_holidays,
    create_holiday,
    update_holiday,
    delete_holiday,
    LeaveError,
    InsufficientBalanceError,
    OverlappingLeaveError,
    SelfReviewError,
    AlreadyReviewedError,
    NotDirectManagerError,
    UnauthorizedAccessError,
)


class TestLeaveServices(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = Session()
        seed_demo_data(self.db)
        self.alice = self.db.query(Employee).filter(Employee.email == "alice@company.com").first()
        self.bob = self.db.query(Employee).filter(Employee.email == "bob@company.com").first()
        self.carol = self.db.query(Employee).filter(Employee.email == "carol@company.com").first()

    def tearDown(self):
        self.db.rollback()
        self.db.close()
        self.engine.dispose()

    # ── Helper ──────────────────────────────────────────────────────────────

    def _bobs_balance(self, leave_type=LeaveType.ANNUAL):
        year = date.today().year
        return (
            self.db.query(LeaveBalance)
            .filter(
                LeaveBalance.employee_id == self.bob.id,
                LeaveBalance.leave_type == leave_type.value,
                LeaveBalance.year == year,
            )
            .first()
        )

    # ── 5.1 Create: success ─────────────────────────────────────────────────

    def test_create_leave_request_success(self):
        """Bob requests 3-day annual leave → balance deducted by working days."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),  # Mon
            end_date=date(2026, 5, 13),    # Wed
            duration=LeaveDuration.FULL,
            reason="Family vacation",
        )
        self.assertEqual(lr.employee_id, self.bob.id)
        self.assertEqual(lr.leave_type, "annual")
        self.assertEqual(lr.status, "pending")
        self.assertEqual(lr.duration, "full")
        self.assertEqual(lr.reason, "Family vacation")

        balance = self._bobs_balance()
        self.assertEqual(balance.used_days, 3.0)
        self.assertEqual(balance.remaining_days, 11.0)

    # ── 5.2 Create: half-day ────────────────────────────────────────────────

    def test_create_leave_request_half_day(self):
        """Bob requests first_half single day → deducts 0.5."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),  # Mon
            end_date=date(2026, 5, 11),
            duration=LeaveDuration.FIRST_HALF,
        )
        self.assertEqual(lr.duration, "first_half")

        balance = self._bobs_balance()
        self.assertEqual(balance.used_days, 0.5)
        self.assertEqual(balance.remaining_days, 13.5)

    # ── 5.3 Create: insufficient balance ────────────────────────────────────

    def test_create_leave_request_insufficient_balance(self):
        """Bob requests 15 working days when only 14 annual days available."""
        with self.assertRaises(InsufficientBalanceError):
            create_leave_request(
                self.db,
                employee_id=self.bob.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 5, 11),   # Mon
                end_date=date(2026, 6, 2),       # Tue — 15 working days
                duration=LeaveDuration.FULL,
            )

        # Balance unchanged after rejection
        balance = self._bobs_balance()
        self.assertEqual(balance.used_days, 0.0)

    # ── 5.4 Create: overlapping ─────────────────────────────────────────────

    def test_create_leave_request_overlapping(self):
        """Second request overlaps with existing pending request."""
        create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        with self.assertRaises(OverlappingLeaveError):
            create_leave_request(
                self.db,
                employee_id=self.bob.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 5, 12),
                end_date=date(2026, 5, 14),
                duration=LeaveDuration.FULL,
            )

    # ── 5.5 Create: half-day same-day collision ─────────────────────────────

    def test_create_leave_request_half_day_same_day_collision(self):
        """AM + PM half-day on same date should collide."""
        create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 11),
            duration=LeaveDuration.FIRST_HALF,
        )

        with self.assertRaises(OverlappingLeaveError) as ctx:
            create_leave_request(
                self.db,
                employee_id=self.bob.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 5, 11),
                end_date=date(2026, 5, 11),
                duration=LeaveDuration.SECOND_HALF,
            )
        self.assertIn("Cancel the existing half-day", str(ctx.exception))

    # ── 5.6 Create: start > end ─────────────────────────────────────────────

    def test_create_leave_request_start_after_end(self):
        with self.assertRaises(LeaveError) as ctx:
            create_leave_request(
                self.db,
                employee_id=self.bob.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 5, 13),
                end_date=date(2026, 5, 11),
                duration=LeaveDuration.FULL,
            )
        self.assertIn("Start date", str(ctx.exception))

    # ── 5.7 Create: backdating ──────────────────────────────────────────────

    def test_create_leave_request_backdating(self):
        with self.assertRaises(LeaveError) as ctx:
            create_leave_request(
                self.db,
                employee_id=self.bob.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 5),
                duration=LeaveDuration.FULL,
            )
        self.assertIn("back-date", str(ctx.exception))

    # ── 5.8 Create: cross-year ──────────────────────────────────────────────

    def test_create_leave_request_cross_year(self):
        with self.assertRaises(LeaveError) as ctx:
            create_leave_request(
                self.db,
                employee_id=self.bob.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 12, 28),
                end_date=date(2027, 1, 4),
                duration=LeaveDuration.FULL,
            )
        self.assertIn("Cross-year", str(ctx.exception))

    # ── 5.9 Create: half-day multi-day ──────────────────────────────────────

    def test_create_leave_request_half_day_multi_day(self):
        with self.assertRaises(LeaveError) as ctx:
            create_leave_request(
                self.db,
                employee_id=self.bob.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 5, 11),
                end_date=date(2026, 5, 12),
                duration=LeaveDuration.FIRST_HALF,
            )
        self.assertIn("single day", str(ctx.exception))

    # ── 5.10 Create: half-day on weekend ────────────────────────────────────

    def test_create_leave_request_half_day_on_weekend(self):
        with self.assertRaises(LeaveError) as ctx:
            create_leave_request(
                self.db,
                employee_id=self.bob.id,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 5, 16),  # Saturday
                end_date=date(2026, 5, 16),
                duration=LeaveDuration.FIRST_HALF,
            )
        self.assertIn("non-working day", str(ctx.exception))

    # ── 5.11 Create: unpaid leave ───────────────────────────────────────────

    def test_create_leave_request_unpaid(self):
        """Unpaid leave with total_days=0 → succeeds, remaining goes negative."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.UNPAID,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )
        self.assertEqual(lr.status, "pending")

        balance = (
            self.db.query(LeaveBalance)
            .filter(
                LeaveBalance.employee_id == self.bob.id,
                LeaveBalance.leave_type == LeaveType.UNPAID.value,
                LeaveBalance.year == 2026,
            )
            .first()
        )
        self.assertEqual(balance.used_days, 3.0)
        self.assertEqual(balance.remaining_days, -3.0)

    # ── 5.12 Create: no balance row ─────────────────────────────────────────

    def test_create_leave_request_no_balance_row(self):
        """Leave type with no balance row should be rejected."""
        # Delete Bob's personal balance to simulate missing row
        self.db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == self.bob.id,
            LeaveBalance.leave_type == LeaveType.PERSONAL.value,
        ).delete()
        self.db.commit()

        with self.assertRaises(LeaveError) as ctx:
            create_leave_request(
                self.db,
                employee_id=self.bob.id,
                leave_type=LeaveType.PERSONAL,
                start_date=date(2026, 5, 11),
                end_date=date(2026, 5, 13),
                duration=LeaveDuration.FULL,
            )
        self.assertIn("No leave balance", str(ctx.exception))

    # ── 5.13 Create: employee not found ─────────────────────────────────────

    def test_create_leave_request_employee_not_found(self):
        with self.assertRaises(LeaveError) as ctx:
            create_leave_request(
                self.db,
                employee_id=9999,
                leave_type=LeaveType.ANNUAL,
                start_date=date(2026, 5, 11),
                end_date=date(2026, 5, 13),
                duration=LeaveDuration.FULL,
            )
        self.assertIn("not found", str(ctx.exception))

    # ── 5.14 Review: approve ────────────────────────────────────────────────

    def test_review_approve(self):
        """Alice approves Bob's pending request → status approved, balance unchanged."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        balance_before = self._bobs_balance().used_days

        result = review_leave_request(
            self.db,
            leave_request_id=lr.id,
            reviewer_id=self.alice.id,
            decision="approved",
        )
        self.assertEqual(result.status, "approved")
        self.assertEqual(result.reviewed_by, self.alice.id)
        self.assertIsNotNone(result.reviewed_at)

        # Balance unchanged (already deducted at creation)
        balance_after = self._bobs_balance().used_days
        self.assertEqual(balance_after, balance_before)

    # ── 5.15 Review: reject restores balance ────────────────────────────────

    def test_review_reject_restores_balance(self):
        """Alice rejects Bob's pending → status rejected, used_days restored."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        result = review_leave_request(
            self.db,
            leave_request_id=lr.id,
            reviewer_id=self.alice.id,
            decision="rejected",
            rejection_reason="Team needs coverage",
        )
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.rejection_reason, "Team needs coverage")

        balance = self._bobs_balance()
        self.assertEqual(balance.used_days, 0.0)
        self.assertEqual(balance.remaining_days, 14.0)

    # ── 5.16 Review: self-review blocked ────────────────────────────────────

    def test_review_self_review_blocked(self):
        """Bob tries to review own request → error (Bob has manager)."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        with self.assertRaises(SelfReviewError):
            review_leave_request(
                self.db,
                leave_request_id=lr.id,
                reviewer_id=self.bob.id,
                decision="approved",
            )

    # ── 5.17 Review: self-review top-level allowed ──────────────────────────

    def test_review_self_review_top_level_allowed(self):
        """Alice (manager_id=NULL) reviews own request → succeeds."""
        lr = create_leave_request(
            self.db,
            employee_id=self.alice.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        result = review_leave_request(
            self.db,
            leave_request_id=lr.id,
            reviewer_id=self.alice.id,
            decision="approved",
        )
        self.assertEqual(result.status, "approved")

    # ── 5.18 Review: not direct manager ─────────────────────────────────────

    def test_review_not_direct_manager(self):
        """Carol tries to review Bob's request → error (Carol is not Bob's manager)."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        with self.assertRaises(NotDirectManagerError):
            review_leave_request(
                self.db,
                leave_request_id=lr.id,
                reviewer_id=self.carol.id,
                decision="approved",
            )

    # ── 5.19 Review: already reviewed ───────────────────────────────────────

    def test_review_already_reviewed(self):
        """Approve, then approve again → error (status not pending)."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        review_leave_request(
            self.db,
            leave_request_id=lr.id,
            reviewer_id=self.alice.id,
            decision="approved",
        )

        with self.assertRaises(AlreadyReviewedError):
            review_leave_request(
                self.db,
                leave_request_id=lr.id,
                reviewer_id=self.alice.id,
                decision="approved",
            )

    # ── 5.20 Cancel: pending ────────────────────────────────────────────────

    def test_cancel_pending(self):
        """Bob cancels own pending request → status cancelled, balance restored."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        result = cancel_leave_request(self.db, leave_request_id=lr.id, employee_id=self.bob.id)
        self.assertEqual(result.status, "cancelled")

        balance = self._bobs_balance()
        self.assertEqual(balance.used_days, 0.0)
        self.assertEqual(balance.remaining_days, 14.0)

    # ── 5.21 Cancel: approved ───────────────────────────────────────────────

    def test_cancel_approved(self):
        """Approve then cancel → status cancelled, balance restored."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        review_leave_request(
            self.db,
            leave_request_id=lr.id,
            reviewer_id=self.alice.id,
            decision="approved",
        )

        result = cancel_leave_request(self.db, leave_request_id=lr.id, employee_id=self.bob.id)
        self.assertEqual(result.status, "cancelled")

        balance = self._bobs_balance()
        self.assertEqual(balance.used_days, 0.0)

    # ── 5.22 Cancel: not owner ──────────────────────────────────────────────

    def test_cancel_not_owner(self):
        """Carol tries to cancel Bob's request → error."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        with self.assertRaises(LeaveError) as ctx:
            cancel_leave_request(self.db, leave_request_id=lr.id, employee_id=self.carol.id)
        self.assertIn("own leave", str(ctx.exception))

    # ── 5.23 Cancel: rejected ───────────────────────────────────────────────

    def test_cancel_rejected(self):
        """Rejected request → cannot cancel."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        review_leave_request(
            self.db,
            leave_request_id=lr.id,
            reviewer_id=self.alice.id,
            decision="rejected",
        )

        with self.assertRaises(LeaveError) as ctx:
            cancel_leave_request(self.db, leave_request_id=lr.id, employee_id=self.bob.id)
        self.assertIn("only pending or approved", str(ctx.exception).lower())

    # ── 5.24 Cancel: already cancelled ──────────────────────────────────────

    def test_cancel_already_cancelled(self):
        """Already cancelled → cannot cancel again."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        cancel_leave_request(self.db, leave_request_id=lr.id, employee_id=self.bob.id)

        with self.assertRaises(LeaveError):
            cancel_leave_request(self.db, leave_request_id=lr.id, employee_id=self.bob.id)

    # ── 5.25 Cancel: past start_date ────────────────────────────────────────

    def test_cancel_past_start_date(self):
        """Cannot cancel if start_date is in the past."""
        lr = create_leave_request(
            self.db,
            employee_id=self.bob.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11),
            end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )

        # Manually set start_date to yesterday
        yesterday = date.today() - date.resolution
        self.db.execute(
            sa_update(LeaveRequest)
            .where(LeaveRequest.id == lr.id)
            .values(start_date=yesterday, end_date=yesterday)
        )
        self.db.commit()

        with self.assertRaises(LeaveError) as ctx:
            cancel_leave_request(self.db, leave_request_id=lr.id, employee_id=self.bob.id)
        self.assertIn("already started", str(ctx.exception))

    # ── 5.26 List: scoping ──────────────────────────────────────────────────

    def test_get_leave_requests_scoped(self):
        """Alice (manager) sees Bob + Carol + self; Bob sees only self."""
        # Bob and Carol each create a leave request
        create_leave_request(
            self.db, employee_id=self.bob.id, leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11), end_date=date(2026, 5, 13), duration=LeaveDuration.FULL,
        )
        create_leave_request(
            self.db, employee_id=self.carol.id, leave_type=LeaveType.SICK,
            start_date=date(2026, 5, 14), end_date=date(2026, 5, 15), duration=LeaveDuration.FULL,
        )

        alice_items, alice_total = get_leave_requests(self.db, caller_id=self.alice.id)
        self.assertEqual(alice_total, 2)

        bob_items, bob_total = get_leave_requests(self.db, caller_id=self.bob.id)
        self.assertEqual(bob_total, 1)
        self.assertEqual(bob_items[0].employee_id, self.bob.id)

    # ── 5.27 List: date filter overlap ──────────────────────────────────────

    def test_get_leave_requests_date_filter_overlap(self):
        """Request July 6-10; filter from July 7 to July 8 → should match (overlap)."""
        create_leave_request(
            self.db, employee_id=self.bob.id, leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 7, 6), end_date=date(2026, 7, 10),
            duration=LeaveDuration.FULL,
        )

        items, total = get_leave_requests(
            self.db, caller_id=self.alice.id,
            from_date=date(2026, 7, 7), to_date=date(2026, 7, 8),
        )
        self.assertEqual(total, 1)

    # ── 5.28 List: date filter no match ─────────────────────────────────────

    def test_get_leave_requests_date_filter_no_match(self):
        """Request July 6-10; filter from July 13 to July 17 → no match."""
        create_leave_request(
            self.db, employee_id=self.bob.id, leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 7, 6), end_date=date(2026, 7, 10),
            duration=LeaveDuration.FULL,
        )

        items, total = get_leave_requests(
            self.db, caller_id=self.alice.id,
            from_date=date(2026, 7, 13), to_date=date(2026, 7, 17),
        )
        self.assertEqual(total, 0)

    # ── 5.29 List: pagination ───────────────────────────────────────────────

    def test_get_leave_requests_pagination(self):
        """Page 1 size 1, page 2 size 1, page beyond data returns empty."""
        # Bob creates 3 leave requests
        for day in range(11, 18):
            if date(2026, 5, day).weekday() < 5:
                create_leave_request(
                    self.db, employee_id=self.bob.id, leave_type=LeaveType.ANNUAL,
                    start_date=date(2026, 5, day), end_date=date(2026, 5, day),
                    duration=LeaveDuration.FULL,
                )

        p1, t1 = get_leave_requests(self.db, caller_id=self.alice.id, page=1, page_size=1)
        self.assertEqual(len(p1), 1)
        self.assertGreater(t1, 1)

        p2, t2 = get_leave_requests(self.db, caller_id=self.alice.id, page=2, page_size=1)
        self.assertEqual(len(p2), 1)

        # Page beyond data
        p99, t99 = get_leave_requests(self.db, caller_id=self.alice.id, page=99, page_size=20)
        self.assertEqual(len(p99), 0)

    # ── 5.30 List employees: direct reports ─────────────────────────────────

    def test_list_employees_direct_reports(self):
        """Alice (manager) sees Bob + Carol; Bob (non-manager) sees empty."""
        alice_items, alice_total = list_employees(self.db, caller_id=self.alice.id)
        self.assertEqual(alice_total, 2)
        alice_ids = {e.id for e in alice_items}
        self.assertIn(self.bob.id, alice_ids)
        self.assertIn(self.carol.id, alice_ids)

        bob_items, bob_total = list_employees(self.db, caller_id=self.bob.id)
        self.assertEqual(bob_total, 0)

    # ── 5.31 Balances: default year ─────────────────────────────────────────

    def test_get_leave_balances_default_year(self):
        """Year omitted → current year balances."""
        balances = get_leave_balances(self.db, employee_id=self.bob.id)
        self.assertGreater(len(balances), 0)
        current_year = date.today().year
        for b in balances:
            self.assertEqual(b.year, current_year)

    # ── 5.32 Balances: empty ────────────────────────────────────────────────

    def test_get_leave_balances_empty(self):
        """New employee with no balance rows → empty list."""
        # Delete all balance rows for Bob
        self.db.query(LeaveBalance).filter(LeaveBalance.employee_id == self.bob.id).delete()
        self.db.commit()

        balances = get_leave_balances(self.db, employee_id=self.bob.id)
        self.assertEqual(len(balances), 0)

    # ── 5.33 Holiday: create ────────────────────────────────────────────────

    def test_holiday_create(self):
        """Alice (manager) creates holiday → success."""
        holiday = create_holiday(
            self.db,
            holiday_date=date(2026, 7, 4),
            name="Independence Day",
            caller_id=self.alice.id,
        )
        self.assertEqual(holiday.name, "Independence Day")
        self.assertEqual(holiday.date, date(2026, 7, 4))

    # ── 5.34 Holiday: duplicate date ────────────────────────────────────────

    def test_holiday_create_duplicate_date(self):
        """Same date twice → error."""
        create_holiday(
            self.db, holiday_date=date(2026, 7, 4), name="First",
            caller_id=self.alice.id,
        )

        with self.assertRaises(LeaveError) as ctx:
            create_holiday(
                self.db, holiday_date=date(2026, 7, 4), name="Second",
                caller_id=self.alice.id,
            )
        self.assertIn("already exists", str(ctx.exception))

    # ── 5.35 Holiday: non-manager blocked ───────────────────────────────────

    def test_holiday_crud_non_manager(self):
        """Bob (non-manager) tries to create holiday → error."""
        with self.assertRaises(LeaveError) as ctx:
            create_holiday(
                self.db, holiday_date=date(2026, 7, 4), name="Test",
                caller_id=self.bob.id,
            )
        self.assertIn("Only managers", str(ctx.exception))

    # ── 5.36 Holiday: list by year ──────────────────────────────────────────

    def test_holiday_list_by_year(self):
        """Filter holidays by year."""
        items, total = list_holidays(self.db, year=2026)
        self.assertGreater(total, 0)
        for h in items:
            self.assertEqual(h.date.year, 2026)

    # ── 5.37 Holiday: update and delete ─────────────────────────────────────

    def test_holiday_update_delete(self):
        """Update name, delete → 204."""
        holiday = create_holiday(
            self.db, holiday_date=date(2026, 7, 4), name="Original",
            caller_id=self.alice.id,
        )

        updated = update_holiday(
            self.db, holiday_id=holiday.id,
            holiday_date=date(2026, 7, 4), name="Updated",
            caller_id=self.alice.id,
        )
        self.assertEqual(updated.name, "Updated")

        delete_holiday(self.db, holiday_id=holiday.id, caller_id=self.alice.id)
        self.assertIsNone(
            self.db.query(PublicHoliday).filter(PublicHoliday.id == holiday.id).first()
        )

    # ── 5.38 Working days: count ────────────────────────────────────────────

    def test_count_working_days(self):
        """Excludes weekends, excludes holidays, all-weekend range returns 0."""
        # Mon–Wed, full → 3
        self.assertEqual(
            count_working_days(self.db, date(2026, 5, 11), date(2026, 5, 13), LeaveDuration.FULL),
            3.0,
        )

        # Fri–Mon, full → 2 (Fri, Mon; Sat/Sun excluded)
        self.assertEqual(
            count_working_days(self.db, date(2026, 5, 15), date(2026, 5, 18), LeaveDuration.FULL),
            2.0,
        )

        # Wesak Day (May 20, Wed) → 0 working days for a single holiday date
        self.assertEqual(
            count_working_days(self.db, date(2026, 5, 20), date(2026, 5, 20), LeaveDuration.FULL),
            0.0,
        )

        # Sat–Sun → 0
        self.assertEqual(
            count_working_days(self.db, date(2026, 5, 16), date(2026, 5, 17), LeaveDuration.FULL),
            0.0,
        )

        # Half-day → always 0.5 regardless of range length
        self.assertEqual(
            count_working_days(self.db, date(2026, 5, 11), date(2026, 5, 11), LeaveDuration.FIRST_HALF),
            0.5,
        )

    # ── 5.39 Holiday on weekend ─────────────────────────────────────────────

    def test_holiday_on_weekend(self):
        """Holiday on Saturday is allowed (no rejection)."""
        holiday = create_holiday(
            self.db,
            holiday_date=date(2026, 6, 13),  # Saturday
            name="Weekend Holiday",
            caller_id=self.alice.id,
        )
        self.assertEqual(holiday.name, "Weekend Holiday")

        # Verify working-day counter still skips weekends (holiday has no
        # extra effect on an already-non-working day)
        days = count_working_days(
            self.db, date(2026, 6, 12), date(2026, 6, 15), LeaveDuration.FULL,
        )
        # Fri June 12 is a working day, Mon June 15 is a working day
        # Sat June 13 (weekend + holiday), Sun June 14 (weekend)
        self.assertEqual(days, 2.0)

    # ── Additional: get_leave_request scoping ───────────────────────────────

    def test_get_leave_request_by_owner(self):
        """Owner can fetch own leave request."""
        lr = create_leave_request(
            self.db, employee_id=self.bob.id, leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11), end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )
        result = get_leave_request(self.db, leave_request_id=lr.id, caller_id=self.bob.id)
        self.assertEqual(result.id, lr.id)

    def test_get_leave_request_by_manager(self):
        """Manager can fetch direct report's leave request."""
        lr = create_leave_request(
            self.db, employee_id=self.bob.id, leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11), end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )
        result = get_leave_request(self.db, leave_request_id=lr.id, caller_id=self.alice.id)
        self.assertEqual(result.id, lr.id)

    def test_get_leave_request_outsider_blocked(self):
        """Non-owner, non-manager cannot fetch leave request."""
        lr = create_leave_request(
            self.db, employee_id=self.bob.id, leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 5, 11), end_date=date(2026, 5, 13),
            duration=LeaveDuration.FULL,
        )
        with self.assertRaises(LeaveError):
            get_leave_request(self.db, leave_request_id=lr.id, caller_id=self.carol.id)

    # ── Additional: get_employee ────────────────────────────────────────────

    def test_get_employee_found(self):
        emp = get_employee(self.db, employee_id=self.bob.id, caller_id=self.bob.id)
        self.assertIsNotNone(emp)
        self.assertEqual(emp.email, "bob@company.com")

    def test_get_employee_not_found(self):
        emp = get_employee(self.db, employee_id=9999, caller_id=1)
        self.assertIsNone(emp)

    def test_get_employee_outsider_denied(self):
        # Carol tries to view a non-direct-report employee — raises 403
        with self.assertRaises(UnauthorizedAccessError):
            get_employee(self.db, employee_id=self.bob.id, caller_id=self.carol.id)

    # ── Additional: holiday update/delete non-manager ───────────────────────

    def test_holiday_update_non_manager(self):
        holiday = create_holiday(
            self.db, holiday_date=date(2026, 7, 4), name="Test",
            caller_id=self.alice.id,
        )
        with self.assertRaises(LeaveError) as ctx:
            update_holiday(
                self.db, holiday_id=holiday.id,
                holiday_date=date(2026, 7, 4), name="Hacked",
                caller_id=self.bob.id,
            )
        self.assertIn("Only managers", str(ctx.exception))

    def test_holiday_delete_non_manager(self):
        holiday = create_holiday(
            self.db, holiday_date=date(2026, 7, 4), name="Test",
            caller_id=self.alice.id,
        )
        with self.assertRaises(LeaveError) as ctx:
            delete_holiday(self.db, holiday_id=holiday.id, caller_id=self.bob.id)
        self.assertIn("Only managers", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
