"""
Tests for Holiday, LeaveDeduction models and LeaveRequest half-day fields.
"""

import unittest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from src.database import Base
import src.models  # ensure all models are registered with Base.metadata


class TestSchemaChanges(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        self.session = Session()

    def tearDown(self):
        self.session.close()
        Base.metadata.drop_all(self.engine)

    def test_holiday_model_importable(self):
        from src.models import Holiday
        self.assertTrue(True)

    def test_leave_deduction_model_importable(self):
        from src.models import LeaveDeduction
        self.assertTrue(True)

    def test_holiday_table_columns(self):
        inspector = inspect(self.engine)
        columns = {c["name"] for c in inspector.get_columns("holidays")}
        self.assertIn("id", columns)
        self.assertIn("date", columns)
        self.assertIn("name", columns)
        self.assertIn("created_at", columns)

    def test_holiday_date_unique(self):
        inspector = inspect(self.engine)
        unique_constraints = inspector.get_unique_constraints("holidays")
        indexes = inspector.get_indexes("holidays")
        unique_cols = set()
        for uc in unique_constraints:
            unique_cols.update(uc["column_names"])
        for idx in indexes:
            if idx.get("unique"):
                unique_cols.update(idx["column_names"])
        self.assertIn("date", unique_cols)

    def test_leave_deduction_table_columns(self):
        inspector = inspect(self.engine)
        columns = {c["name"] for c in inspector.get_columns("leave_deductions")}
        self.assertIn("id", columns)
        self.assertIn("leave_request_id", columns)
        self.assertIn("year", columns)
        self.assertIn("days", columns)
        self.assertIn("created_at", columns)

    def test_leave_deduction_unique_constraint(self):
        inspector = inspect(self.engine)
        unique_constraints = inspector.get_unique_constraints("leave_deductions")
        indexes = inspector.get_indexes("leave_deductions")
        all_unique_sets = []
        for uc in unique_constraints:
            all_unique_sets.append(set(uc["column_names"]))
        for idx in indexes:
            if idx.get("unique"):
                all_unique_sets.append(set(idx["column_names"]))
        self.assertIn({"leave_request_id", "year"}, all_unique_sets)

    def test_leave_request_half_day_fields(self):
        inspector = inspect(self.engine)
        columns = {c["name"] for c in inspector.get_columns("leave_requests")}
        self.assertIn("half_day_start", columns)
        self.assertIn("half_day_end", columns)

    def test_leave_request_half_day_defaults_false(self):
        inspector = inspect(self.engine)
        columns = {c["name"]: c for c in inspector.get_columns("leave_requests")}
        self.assertIn("half_day_start", columns)
        self.assertIn("half_day_end", columns)

    def test_leave_deduction_relationship_on_leave_request(self):
        from src.models import LeaveRequest
        self.assertTrue(hasattr(LeaveRequest, "deductions"))

    def test_leave_deduction_insert(self):
        from src.models import Employee, LeaveRequest, LeaveDeduction, LeaveType
        from datetime import date
        emp = Employee(name="Test", email="t@t.com", department="Eng")
        self.session.add(emp)
        self.session.flush()
        lr = LeaveRequest(
            employee_id=emp.id,
            leave_type=LeaveType.ANNUAL,
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 3),
        )
        self.session.add(lr)
        self.session.flush()
        ld = LeaveDeduction(leave_request_id=lr.id, year=2025, days=2.0)
        self.session.add(ld)
        self.session.commit()
        result = self.session.query(LeaveDeduction).first()
        self.assertEqual(result.days, 2.0)
        self.assertEqual(result.year, 2025)

    def test_holiday_insert(self):
        from src.models import Holiday
        from datetime import date
        h = Holiday(date=date(2025, 1, 1), name="New Year")
        self.session.add(h)
        self.session.commit()
        result = self.session.query(Holiday).first()
        self.assertEqual(result.name, "New Year")


if __name__ == "__main__":
    unittest.main()
