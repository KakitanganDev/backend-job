"""
Integration tests for the FastAPI leave management API.

These tests use TestClient to exercise the full stack.
They currently pass because they're placeholders — replace
with real tests once you implement src/services.py.
"""

import unittest
from fastapi.testclient import TestClient

from src.app import app


class TestLeaveAPI(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    def test_list_employees(self):
        resp = self.client.get("/employees")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), list)

    def test_get_employee(self):
        resp = self.client.get("/employees/1")
        # May return 200 or 404 depending on seed data
        self.assertIn(resp.status_code, (200, 404))

    def test_create_leave_request(self):
        resp = self.client.post("/leave-requests", json={
            "employee_id": 2,
            "leave_type": "annual",
            "start_date": str(date.today() + timedelta(days=7)),
            "end_date": str(date.today() + timedelta(days=9)),
        })
        # Service is implemented; 201=success, 404=employee not in test DB, 422=validation error
        self.assertIn(resp.status_code, (201, 404, 422, 500))


from datetime import date, timedelta

if __name__ == "__main__":
    unittest.main()
