"""
Tests for the leave management service layer.
"""

import unittest
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import Employee, LeaveBalance, LeaveType, LeaveStatus
from src.services import (
    seed_demo_data,
    create_leave_request,
    approve_leave_request,
    cancel_leave_request,
    get_leave_requests,
    get_leave_balances,
    InsufficientBalanceError,
    OverlappingLeaveError,
    SelfApprovalError,
    LeaveError,
)

TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
IN_3_DAYS = TODAY + timedelta(days=3)
IN_7_DAYS = TODAY + timedelta(days=7)
IN_10_DAYS = TODAY + timedelta(days=10)
IN_14_DAYS = TODAY + timedelta(days=14)
YESTERDAY = TODAY - timedelta(days=1)


class TestLeaveServices(unittest.TestCase):

    def setUp(self):
        # Fresh in-memory DB per test for full isolation
        from sqlalchemy import create_engine
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        self.db = Session()
        seed_demo_data(self.db)
        self.alice = self.db.query(Employee).filter(Employee.email == "alice@company.com").first()
        self.bob = self.db.query(Employee).filter(Employee.email == "bob@company.com").first()
        self.carol = self.db.query(Employee).filter(Employee.email == "carol@company.com").first()

    def tearDown(self):
        self.db.close()

    # ── create_leave_request ─────────────────────────────────────────────

    def test_create_leave_request_happy_path(self):
        lr = create_leave_request(
            self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS
        )
        self.assertEqual(lr.employee_id, self.bob.id)
        self.assertEqual(lr.leave_type, LeaveType.ANNUAL)
        self.assertEqual(lr.status, LeaveStatus.PENDING)
        self.assertEqual(lr.start_date, TOMORROW)
        self.assertEqual(lr.end_date, IN_3_DAYS)

    def test_create_leave_request_does_not_deduct_balance_on_create(self):
        """Balance deduction happens on approval, not on create."""
        balances_before = get_leave_balances(self.db, self.bob.id)
        annual_before = next(b for b in balances_before if b.leave_type == LeaveType.ANNUAL)
        used_before = annual_before.used_days

        create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)

        balances_after = get_leave_balances(self.db, self.bob.id)
        annual_after = next(b for b in balances_after if b.leave_type == LeaveType.ANNUAL)
        self.assertEqual(annual_after.used_days, used_before)

    def test_create_leave_request_single_day(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.SICK, TOMORROW, TOMORROW)
        self.assertEqual(lr.start_date, lr.end_date)

    def test_create_leave_request_insufficient_balance(self):
        # Bob has 14 annual days; request 20 days
        with self.assertRaises(InsufficientBalanceError):
            create_leave_request(
                self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, TODAY + timedelta(days=21)
            )

    def test_create_leave_request_overlapping_dates(self):
        create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, IN_7_DAYS, IN_10_DAYS)
        with self.assertRaises(OverlappingLeaveError):
            create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, IN_7_DAYS + timedelta(days=1), IN_14_DAYS)

    def test_create_leave_request_overlap_adjacent_is_allowed(self):
        """A request ending on day N and another starting on day N+1 should not overlap."""
        create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, IN_7_DAYS, IN_10_DAYS)
        lr2 = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, IN_10_DAYS + timedelta(days=1), IN_14_DAYS)
        self.assertIsNotNone(lr2.id)

    def test_create_leave_request_backdated_rejected(self):
        with self.assertRaises(LeaveError):
            create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, YESTERDAY, TOMORROW)

    def test_create_leave_request_end_before_start_rejected(self):
        with self.assertRaises(LeaveError):
            create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, IN_7_DAYS, IN_3_DAYS)

    def test_create_leave_request_unknown_employee(self):
        with self.assertRaises(LeaveError):
            create_leave_request(self.db, 9999, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)

    def test_create_leave_request_unpaid_no_balance_needed(self):
        """UNPAID leave should succeed even without a balance record."""
        lr = create_leave_request(self.db, self.bob.id, LeaveType.UNPAID, TOMORROW, IN_7_DAYS)
        self.assertEqual(lr.leave_type, LeaveType.UNPAID)
        self.assertEqual(lr.status, LeaveStatus.PENDING)

    def test_failed_create_does_not_create_zero_balance_row(self):
        balances_before = get_leave_balances(self.db, self.bob.id)
        self.assertFalse(any(b.leave_type == LeaveType.PERSONAL for b in balances_before))

        with self.assertRaises(InsufficientBalanceError):
            create_leave_request(
                self.db, self.bob.id, LeaveType.PERSONAL, TOMORROW, TOMORROW
            )

        balances_after = get_leave_balances(self.db, self.bob.id)
        self.assertFalse(any(b.leave_type == LeaveType.PERSONAL for b in balances_after))

    def test_cancelled_request_does_not_block_overlap(self):
        """A cancelled leave in the same date range should not trigger OverlappingLeaveError."""
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, IN_7_DAYS, IN_10_DAYS)
        cancel_leave_request(self.db, lr.id, self.bob.id)
        lr2 = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, IN_7_DAYS, IN_10_DAYS)
        self.assertEqual(lr2.status, LeaveStatus.PENDING)

    # ── approve_leave_request ────────────────────────────────────────────

    def test_approve_leave_request(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        approved = approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.APPROVED)
        self.assertEqual(approved.status, LeaveStatus.APPROVED)
        self.assertEqual(approved.approved_by, self.alice.id)
        self.assertIsNotNone(approved.approved_at)

    def test_approve_deducts_balance(self):
        days = (IN_3_DAYS - TOMORROW).days + 1  # 3 days
        balances = get_leave_balances(self.db, self.bob.id)
        annual = next(b for b in balances if b.leave_type == LeaveType.ANNUAL)
        used_before = annual.used_days

        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.APPROVED)

        self.db.expire(annual)
        self.assertEqual(annual.used_days, used_before + days)

    def test_reject_leave_request(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        rejected = approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.REJECTED)
        self.assertEqual(rejected.status, LeaveStatus.REJECTED)

    def test_reject_does_not_deduct_balance(self):
        balances = get_leave_balances(self.db, self.bob.id)
        annual = next(b for b in balances if b.leave_type == LeaveType.ANNUAL)
        used_before = annual.used_days

        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.REJECTED)

        self.db.expire(annual)
        self.assertEqual(annual.used_days, used_before)

    def test_self_approval_rejected(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        with self.assertRaises(SelfApprovalError):
            approve_leave_request(self.db, lr.id, self.bob.id, LeaveStatus.APPROVED)

    def test_non_manager_cannot_approve(self):
        """Carol is not Bob's manager, so she cannot approve Bob's leave."""
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        with self.assertRaises(LeaveError):
            approve_leave_request(self.db, lr.id, self.carol.id, LeaveStatus.APPROVED)

    def test_cannot_approve_already_approved(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.APPROVED)
        with self.assertRaises(LeaveError):
            approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.APPROVED)

    def test_cannot_approve_cancelled(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        cancel_leave_request(self.db, lr.id, self.bob.id)
        with self.assertRaises(LeaveError):
            approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.APPROVED)

    def test_approve_nonexistent_request(self):
        with self.assertRaises(LeaveError):
            approve_leave_request(self.db, 9999, self.alice.id, LeaveStatus.APPROVED)

    def test_invalid_decision_rejected(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        with self.assertRaises(LeaveError):
            approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.PENDING)

    # ── cancel_leave_request ─────────────────────────────────────────────

    def test_cancel_pending_leave(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        cancelled = cancel_leave_request(self.db, lr.id, self.bob.id)
        self.assertEqual(cancelled.status, LeaveStatus.CANCELLED)

    def test_cancel_approved_leave_restores_balance(self):
        days = (IN_3_DAYS - TOMORROW).days + 1
        balances = get_leave_balances(self.db, self.bob.id)
        annual = next(b for b in balances if b.leave_type == LeaveType.ANNUAL)
        used_before = annual.used_days

        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.APPROVED)
        cancel_leave_request(self.db, lr.id, self.bob.id)

        self.db.expire(annual)
        self.assertEqual(annual.used_days, used_before)

    def test_cancel_by_non_owner_rejected(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        with self.assertRaises(LeaveError):
            cancel_leave_request(self.db, lr.id, self.carol.id)

    def test_cancel_already_cancelled_rejected(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        cancel_leave_request(self.db, lr.id, self.bob.id)
        with self.assertRaises(LeaveError):
            cancel_leave_request(self.db, lr.id, self.bob.id)

    def test_cancel_rejected_leave_not_allowed(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.REJECTED)
        with self.assertRaises(LeaveError):
            cancel_leave_request(self.db, lr.id, self.bob.id)

    # ── get_leave_requests ───────────────────────────────────────────────

    def test_list_leave_requests_with_filters(self):
        create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        create_leave_request(self.db, self.carol.id, LeaveType.SICK, IN_7_DAYS, IN_10_DAYS)

        items, total = get_leave_requests(self.db, employee_id=self.bob.id)
        self.assertTrue(all(lr.employee_id == self.bob.id for lr in items))

        items, total = get_leave_requests(self.db, leave_type=LeaveType.SICK)
        self.assertTrue(all(lr.leave_type == LeaveType.SICK for lr in items))

    def test_list_leave_requests_pagination(self):
        for i in range(5):
            start = TODAY + timedelta(days=1 + i * 3)
            end = start + timedelta(days=1)
            create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, start, end)

        page1, total = get_leave_requests(self.db, employee_id=self.bob.id, page=1, page_size=2)
        page2, _ = get_leave_requests(self.db, employee_id=self.bob.id, page=2, page_size=2)

        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        self.assertEqual(total, 5)
        ids_p1 = {lr.id for lr in page1}
        ids_p2 = {lr.id for lr in page2}
        self.assertTrue(ids_p1.isdisjoint(ids_p2))

    def test_list_leave_requests_status_filter(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.APPROVED)
        create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, IN_7_DAYS, IN_10_DAYS)

        approved_items, _ = get_leave_requests(self.db, status=LeaveStatus.APPROVED)
        self.assertTrue(all(lr.status == LeaveStatus.APPROVED for lr in approved_items))

        pending_items, _ = get_leave_requests(self.db, status=LeaveStatus.PENDING)
        self.assertTrue(all(lr.status == LeaveStatus.PENDING for lr in pending_items))

    def test_list_leave_requests_date_range_filter(self):
        create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, IN_7_DAYS, IN_10_DAYS)

        items, total = get_leave_requests(self.db, from_date=IN_7_DAYS)
        self.assertEqual(total, 1)
        self.assertEqual(items[0].start_date, IN_7_DAYS)

    def test_list_all_returns_all_employees(self):
        create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        create_leave_request(self.db, self.carol.id, LeaveType.SICK, IN_7_DAYS, IN_10_DAYS)

        _, total = get_leave_requests(self.db)
        self.assertEqual(total, 2)

    # ── get_leave_balances ───────────────────────────────────────────────

    def test_get_leave_balances(self):
        balances = get_leave_balances(self.db, self.bob.id)
        self.assertEqual(len(balances), 2)
        types = {b.leave_type for b in balances}
        self.assertIn(LeaveType.ANNUAL, types)
        self.assertIn(LeaveType.SICK, types)

    def test_get_leave_balances_defaults_to_current_year(self):
        balances = get_leave_balances(self.db, self.bob.id)
        for b in balances:
            self.assertEqual(b.year, date.today().year)

    def test_get_leave_balances_explicit_year(self):
        balances = get_leave_balances(self.db, self.bob.id, year=date.today().year)
        self.assertGreater(len(balances), 0)

    def test_get_leave_balances_unknown_year_returns_empty(self):
        balances = get_leave_balances(self.db, self.bob.id, year=1900)
        self.assertEqual(balances, [])

    def test_remaining_days_computed_correctly(self):
        lr = create_leave_request(self.db, self.bob.id, LeaveType.ANNUAL, TOMORROW, IN_3_DAYS)
        approve_leave_request(self.db, lr.id, self.alice.id, LeaveStatus.APPROVED)

        balances = get_leave_balances(self.db, self.bob.id)
        annual = next(b for b in balances if b.leave_type == LeaveType.ANNUAL)
        days_taken = (IN_3_DAYS - TOMORROW).days + 1
        self.assertEqual(annual.remaining_days, 14.0 - days_taken)


if __name__ == "__main__":
    unittest.main()
