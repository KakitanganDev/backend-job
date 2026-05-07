"""
Integration tests for the FastAPI leave management API.

These tests use TestClient to exercise the full stack.
"""

import os
import unittest
from datetime import date, timedelta

os.environ["DATABASE_URL"] = "sqlite:///./test_api.db"

from fastapi.testclient import TestClient

from src.database import Base, engine, SessionLocal
from src.models import LeaveRequest, PublicHoliday
from src.services import seed_demo_data
from src.app import app


class TestLeaveAPI(unittest.TestCase):

    _db_path = "./test_api.db"

    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            seed_demo_data(db)
        finally:
            db.close()

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        if os.path.exists(cls._db_path):
            os.remove(cls._db_path)

    def setUp(self):
        self.client = TestClient(app)
        db = SessionLocal()
        try:
            db.query(LeaveRequest).delete()
            from src.models import LeaveBalance
            db.query(LeaveBalance).update({"used_days": 0.0})
            db.commit()
        finally:
            db.close()

    def test_auth_missing_header(self):
        resp = self.client.get("/api/v1/employees")
        self.assertEqual(resp.status_code, 401)

    def test_auth_malformed_header(self):
        resp = self.client.get("/api/v1/employees", headers={"Authorization": "Invalid thing"})
        self.assertEqual(resp.status_code, 401)

    def test_list_employees(self):
        resp = self.client.get("/api/v1/employees", headers={"Authorization": "Bearer 1"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("items", data)
        self.assertIn("total", data)

    def test_get_employee(self):
        resp = self.client.get("/api/v1/employees/2", headers={"Authorization": "Bearer 1"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("employee", data)
        self.assertIn("leave_balances", data)

    def test_get_employee_404(self):
        resp = self.client.get("/api/v1/employees/9999", headers={"Authorization": "Bearer 1"})
        self.assertEqual(resp.status_code, 404)

    def test_get_employee_403(self):
        # Carol (id=3) is not Bob's (id=2) manager — should be denied
        resp = self.client.get("/api/v1/employees/2", headers={"Authorization": "Bearer 3"})
        self.assertEqual(resp.status_code, 403)

    def test_create_leave_request(self):
        resp = self.client.post(
            "/api/v1/leave-requests",
            json={
                "leave_type": "annual",
                "start_date": str(date.today() + timedelta(days=7)),
                "end_date": str(date.today() + timedelta(days=9)),
                "duration": "full",
            },
            headers={"Authorization": "Bearer 2"},
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["leave_type"], "annual")

    def test_create_leave_request_validation_error(self):
        resp = self.client.post(
            "/api/v1/leave-requests",
            json={
                "leave_type": "annual",
                "start_date": str(date.today() + timedelta(days=9)),
                "end_date": str(date.today() + timedelta(days=7)),
                "duration": "full",
            },
            headers={"Authorization": "Bearer 2"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_review_leave_request(self):
        # Create a request as Bob
        create_resp = self.client.post(
            "/api/v1/leave-requests",
            json={
                "leave_type": "annual",
                "start_date": str(date.today() + timedelta(days=7)),
                "end_date": str(date.today() + timedelta(days=9)),
                "duration": "full",
            },
            headers={"Authorization": "Bearer 2"},
        )
        self.assertEqual(create_resp.status_code, 201)
        lr_id = create_resp.json()["id"]

        # Alice (manager) reviews
        resp = self.client.post(
            f"/api/v1/leave-requests/{lr_id}/review",
            json={"decision": "approved"},
            headers={"Authorization": "Bearer 1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "approved")

    def test_cancel_leave_request(self):
        # Create a request as Bob
        create_resp = self.client.post(
            "/api/v1/leave-requests",
            json={
                "leave_type": "annual",
                "start_date": str(date.today() + timedelta(days=7)),
                "end_date": str(date.today() + timedelta(days=9)),
                "duration": "full",
            },
            headers={"Authorization": "Bearer 2"},
        )
        self.assertEqual(create_resp.status_code, 201)
        lr_id = create_resp.json()["id"]

        resp = self.client.post(
            f"/api/v1/leave-requests/{lr_id}/cancel",
            headers={"Authorization": "Bearer 2"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "cancelled")

    def test_cancel_not_owner(self):
        # Create as Bob
        create_resp = self.client.post(
            "/api/v1/leave-requests",
            json={
                "leave_type": "annual",
                "start_date": str(date.today() + timedelta(days=7)),
                "end_date": str(date.today() + timedelta(days=9)),
                "duration": "full",
            },
            headers={"Authorization": "Bearer 2"},
        )
        self.assertEqual(create_resp.status_code, 201)
        lr_id = create_resp.json()["id"]

        # Carol tries to cancel Bob's request
        resp = self.client.post(
            f"/api/v1/leave-requests/{lr_id}/cancel",
            headers={"Authorization": "Bearer 3"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_leave_balances(self):
        resp = self.client.get("/api/v1/leave-balances/2", headers={"Authorization": "Bearer 1"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)

    def test_holiday_crud(self):
        # Create as Alice (manager)
        create_resp = self.client.post(
            "/api/v1/holidays",
            json={"date": "2026-07-04", "name": "Test Holiday"},
            headers={"Authorization": "Bearer 1"},
        )
        self.assertEqual(create_resp.status_code, 201)

        holiday_id = create_resp.json()["id"]

        # List
        list_resp = self.client.get("/api/v1/holidays", headers={"Authorization": "Bearer 2"})
        self.assertEqual(list_resp.status_code, 200)

        # Update
        update_resp = self.client.put(
            f"/api/v1/holidays/{holiday_id}",
            json={"date": "2026-07-04", "name": "Updated Holiday"},
            headers={"Authorization": "Bearer 1"},
        )
        self.assertEqual(update_resp.status_code, 200)

        # Delete
        delete_resp = self.client.delete(
            f"/api/v1/holidays/{holiday_id}",
            headers={"Authorization": "Bearer 1"},
        )
        self.assertEqual(delete_resp.status_code, 204)

    def test_holiday_unauthorized(self):
        resp = self.client.post(
            "/api/v1/holidays",
            json={"date": "2026-07-04", "name": "Test"},
            headers={"Authorization": "Bearer 2"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_pagination_edge_cases(self):
        resp = self.client.get(
            "/api/v1/employees",
            params={"page": 0},
            headers={"Authorization": "Bearer 1"},
        )
        self.assertEqual(resp.status_code, 422)

        resp = self.client.get(
            "/api/v1/employees",
            params={"page": 999},
            headers={"Authorization": "Bearer 1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["items"]), 0)


if __name__ == "__main__":
    unittest.main()
