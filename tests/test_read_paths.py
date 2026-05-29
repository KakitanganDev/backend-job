"""
Tests for read-path service functions: get_leave_requests and get_leave_balances.

Note: We avoid instantiating Employee ORM objects because the models in this
worktree have an ambiguous foreign_keys configuration on Employee.leave_requests
(two FK paths: employee_id and approved_by). LeaveRequest and LeaveBalance rows
are inserted via Core INSERT to bypass mapper configuration issues.
"""

import pytest
from datetime import date
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import LeaveRequest, LeaveBalance, LeaveType, LeaveStatus
from src.services import get_leave_requests, get_leave_balances


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture(scope="module")
def Session(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db(Session):
    session = Session()
    yield session
    session.rollback()
    session.close()


# ---------------------------------------------------------------------------
# get_leave_requests — empty DB
# ---------------------------------------------------------------------------


class TestGetLeaveRequestsEmptyDB:
    def test_returns_empty_list_and_zero_on_empty_db(self, db):
        items, total = get_leave_requests(db)
        assert items == []
        assert total == 0


# ---------------------------------------------------------------------------
# get_leave_requests — with data (use employee_ids 1001 and 1002 as sentinels)
# ---------------------------------------------------------------------------

EMP1 = 1001
EMP2 = 1002


@pytest.fixture
def leave_requests(db):
    """Insert three leave request rows directly via Core to sidestep mapper config issues."""
    r1 = db.execute(
        insert(LeaveRequest).values(
            employee_id=EMP1,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 5),
            status=LeaveStatus.PENDING,
        )
    )
    r2 = db.execute(
        insert(LeaveRequest).values(
            employee_id=EMP2,
            leave_type=LeaveType.SICK,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 3),
            status=LeaveStatus.APPROVED,
        )
    )
    r3 = db.execute(
        insert(LeaveRequest).values(
            employee_id=EMP1,
            leave_type=LeaveType.SICK,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 2),
            status=LeaveStatus.REJECTED,
        )
    )
    db.flush()
    ids = [r1.inserted_primary_key[0], r2.inserted_primary_key[0], r3.inserted_primary_key[0]]
    yield ids
    db.execute(
        LeaveRequest.__table__.delete().where(LeaveRequest.id.in_(ids))
    )
    db.commit()


class TestGetLeaveRequestsFiltering:
    def test_filter_by_employee_id(self, db, leave_requests):
        items, total = get_leave_requests(db, employee_id=EMP2)
        assert total == 1
        assert len(items) == 1
        assert items[0].employee_id == EMP2

    def test_filter_by_status(self, db, leave_requests):
        items, total = get_leave_requests(db, status=LeaveStatus.APPROVED)
        assert total == 1
        assert items[0].status == LeaveStatus.APPROVED

    def test_filter_by_leave_type(self, db, leave_requests):
        items, total = get_leave_requests(db, leave_type=LeaveType.ANNUAL)
        assert total == 1
        assert items[0].leave_type == LeaveType.ANNUAL

    def test_filter_from_date(self, db, leave_requests):
        # from_date=2026-08-01 includes rows where start_date >= 2026-08-01 (r2, r3)
        items, total = get_leave_requests(db, from_date=date(2026, 8, 1))
        assert total == 2
        employee_ids = {item.employee_id for item in items}
        assert EMP2 in employee_ids
        assert EMP1 in employee_ids

    def test_filter_to_date(self, db, leave_requests):
        # to_date=2026-07-31 includes rows where end_date <= 2026-07-31 (r1 only)
        items, total = get_leave_requests(db, to_date=date(2026, 7, 31))
        assert total == 1
        assert items[0].employee_id == EMP1
        assert items[0].leave_type == LeaveType.ANNUAL

    def test_total_count_reflects_all_matching_rows_not_page(self, db, leave_requests):
        # EMP1 has 2 requests; page_size=1 but total must still be 2
        items, total = get_leave_requests(db, employee_id=EMP1, page=1, page_size=1)
        assert total == 2
        assert len(items) == 1

    def test_pagination_second_page(self, db, leave_requests):
        items_p1, _ = get_leave_requests(db, employee_id=EMP1, page=1, page_size=1)
        items_p2, _ = get_leave_requests(db, employee_id=EMP1, page=2, page_size=1)
        assert len(items_p1) == 1
        assert len(items_p2) == 1
        assert items_p1[0].id != items_p2[0].id

    def test_no_filters_returns_all(self, db, leave_requests):
        _, total = get_leave_requests(db)
        assert total == 3


# ---------------------------------------------------------------------------
# get_leave_balances — no rows exist for employee
# ---------------------------------------------------------------------------


class TestGetLeaveBalancesNoRows:
    def test_returns_six_synthesized_rows_for_unknown_employee(self, db):
        results = get_leave_balances(db, employee_id=9999, year=2026)
        assert len(results) == len(LeaveType)

    def test_synthesized_rows_have_zero_days(self, db):
        results = get_leave_balances(db, employee_id=9999, year=2026)
        for row in results:
            assert row.total_days == 0
            assert row.used_days == 0
            assert row.remaining_days == 0

    def test_synthesized_rows_correct_employee_id(self, db):
        results = get_leave_balances(db, employee_id=9999, year=2026)
        for row in results:
            assert row.employee_id == 9999

    def test_synthesized_rows_correct_year(self, db):
        results = get_leave_balances(db, employee_id=9999, year=2026)
        for row in results:
            assert row.year == 2026

    def test_covers_all_leave_types(self, db):
        results = get_leave_balances(db, employee_id=9999, year=2026)
        returned_types = {row.leave_type for row in results}
        assert returned_types == set(LeaveType)


# ---------------------------------------------------------------------------
# get_leave_balances — one real row exists
# ---------------------------------------------------------------------------


@pytest.fixture
def annual_balance(db):
    """Insert a single LeaveBalance row for EMP1, ANNUAL, 2026."""
    result = db.execute(
        insert(LeaveBalance).values(
            employee_id=EMP1,
            leave_type=LeaveType.ANNUAL,
            year=2026,
            total_days=14,
            used_days=3,
        )
    )
    db.flush()
    bal_id = result.inserted_primary_key[0]
    yield bal_id
    db.execute(LeaveBalance.__table__.delete().where(LeaveBalance.id == bal_id))
    db.commit()


class TestGetLeaveBalancesWithRealRows:
    def test_returns_six_rows_when_one_real_exists(self, db, annual_balance):
        results = get_leave_balances(db, employee_id=EMP1, year=2026)
        assert len(results) == len(LeaveType)

    def test_real_row_values_preserved(self, db, annual_balance):
        results = get_leave_balances(db, employee_id=EMP1, year=2026)
        annual = next(r for r in results if r.leave_type == LeaveType.ANNUAL)
        assert annual.total_days == 14
        assert annual.used_days == 3
        assert annual.remaining_days == 11

    def test_missing_types_are_synthesized_as_zero(self, db, annual_balance):
        results = get_leave_balances(db, employee_id=EMP1, year=2026)
        for row in results:
            if row.leave_type != LeaveType.ANNUAL:
                assert row.total_days == 0
                assert row.used_days == 0

    def test_synthesized_rows_not_persisted(self, db, annual_balance):
        get_leave_balances(db, employee_id=EMP1, year=2026)
        count = (
            db.query(LeaveBalance)
            .filter(
                LeaveBalance.employee_id == EMP1,
                LeaveBalance.year == 2026,
            )
            .count()
        )
        # Only the one real row should be in DB
        assert count == 1


# ---------------------------------------------------------------------------
# get_leave_balances — default year
# ---------------------------------------------------------------------------


class TestGetLeaveBalancesDefaultYear:
    def test_defaults_to_current_year(self, db):
        current_year = date.today().year
        results = get_leave_balances(db, employee_id=8888)
        for row in results:
            assert row.year == current_year
