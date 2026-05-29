"""
Tests for API wiring: schemas, error mapping, and route updates.
"""

import unittest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from src.app import app
from src import services
from src.models import LeaveType, LeaveStatus


def _make_leave_request(
    id=1,
    employee_id=2,
    leave_type=LeaveType.ANNUAL,
    start_date=None,
    end_date=None,
    status=LeaveStatus.PENDING,
    approved_by=None,
    approved_at=None,
    reason=None,
    half_day_start=False,
    half_day_end=False,
):
    """Build a mock LeaveRequest ORM object."""
    lr = MagicMock()
    lr.id = id
    lr.employee_id = employee_id
    lr.leave_type = leave_type
    lr.start_date = start_date or (date.today() + timedelta(days=7))
    lr.end_date = end_date or (date.today() + timedelta(days=9))
    lr.status = status
    lr.approved_by = approved_by
    lr.approved_at = approved_at
    lr.reason = reason
    lr.half_day_start = half_day_start
    lr.half_day_end = half_day_end
    lr._estimated_deductions = [{"leave_type": "annual", "days": 3.0}]
    lr._deductions = [{"leave_type": "annual", "days": 3.0}]
    lr._restored_deductions = [{"leave_type": "annual", "days": 3.0}]
    return lr


class TestCreateLeaveRequestEstimatedDeductions(unittest.TestCase):
    """AC1: POST /leave-requests response includes estimated_deductions array."""

    def setUp(self):
        self.client = TestClient(app)

    def test_create_leave_request_returns_estimated_deductions(self):
        lr = _make_leave_request()
        with patch("src.services.create_leave_request", return_value=lr):
            resp = self.client.post("/leave-requests", json={
                "employee_id": 2,
                "leave_type": "annual",
                "start_date": str(date.today() + timedelta(days=7)),
                "end_date": str(date.today() + timedelta(days=9)),
            })
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("estimated_deductions", data)
        self.assertIsInstance(data["estimated_deductions"], list)
        self.assertEqual(data["estimated_deductions"], [{"leave_type": "annual", "days": 3.0}])


class TestErrorResponseCodeField(unittest.TestCase):
    """AC3/AC4: Error responses return JSON with both detail and code fields."""

    def setUp(self):
        self.client = TestClient(app)

    def test_backdated_request_returns_code_field(self):
        with patch("src.services.create_leave_request",
                   side_effect=services.BackdatedRequestError("Start date cannot be in the past")):
            resp = self.client.post("/leave-requests", json={
                "employee_id": 2,
                "leave_type": "annual",
                "start_date": "2020-01-01",
                "end_date": "2020-01-03",
            })
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertIn("detail", body)
        self.assertIn("code", body["detail"])
        self.assertEqual(body["detail"]["code"], "BACKDATED_REQUEST")

    def test_error_response_has_detail_and_code(self):
        with patch("src.services.create_leave_request",
                   side_effect=services.InsufficientBalanceError("Not enough balance")):
            resp = self.client.post("/leave-requests", json={
                "employee_id": 2,
                "leave_type": "annual",
                "start_date": str(date.today() + timedelta(days=7)),
                "end_date": str(date.today() + timedelta(days=9)),
            })
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        detail = body["detail"]
        self.assertIn("detail", detail)
        self.assertIn("code", detail)
        self.assertEqual(detail["code"], "INSUFFICIENT_BALANCE")

    def test_employee_not_found_returns_404_with_code(self):
        with patch("src.services.create_leave_request",
                   side_effect=services.EmployeeNotFoundError("Employee not found")):
            resp = self.client.post("/leave-requests", json={
                "employee_id": 999,
                "leave_type": "annual",
                "start_date": str(date.today() + timedelta(days=7)),
                "end_date": str(date.today() + timedelta(days=9)),
            })
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body["detail"]["code"], "EMPLOYEE_NOT_FOUND")


class TestReviewRequiresApproverId(unittest.TestCase):
    """AC2: POST /leave-requests/{id}/review body requires approver_id field."""

    def setUp(self):
        self.client = TestClient(app)

    def test_review_without_approver_id_returns_422(self):
        resp = self.client.post("/leave-requests/1/review", json={
            "decision": "approved",
        })
        self.assertEqual(resp.status_code, 422)

    def test_review_with_approver_id_calls_service_correctly(self):
        lr = _make_leave_request(status=LeaveStatus.APPROVED, approved_by=1)
        lr._deductions = [{"leave_type": "annual", "days": 3.0}]
        with patch("src.services.approve_leave_request", return_value=lr) as mock_approve:
            resp = self.client.post("/leave-requests/1/review", json={
                "approver_id": 1,
                "decision": "approved",
            })
        self.assertEqual(resp.status_code, 200)
        mock_approve.assert_called_once()
        call_kwargs = mock_approve.call_args[1]
        self.assertEqual(call_kwargs["approver_id"], 1)

    def test_review_passes_approver_id_not_hardcoded(self):
        lr = _make_leave_request(status=LeaveStatus.APPROVED, approved_by=5)
        lr._deductions = [{"leave_type": "annual", "days": 3.0}]
        with patch("src.services.approve_leave_request", return_value=lr) as mock_approve:
            resp = self.client.post("/leave-requests/1/review", json={
                "approver_id": 5,
                "decision": "approved",
            })
        self.assertEqual(resp.status_code, 200)
        call_kwargs = mock_approve.call_args[1]
        self.assertEqual(call_kwargs["approver_id"], 5)


if __name__ == "__main__":
    unittest.main()
