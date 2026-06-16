"""
Integration tests for the FastAPI leave management API.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
IN_3_DAYS = TODAY + timedelta(days=3)
IN_7_DAYS = TODAY + timedelta(days=7)
IN_10_DAYS = TODAY + timedelta(days=10)


def build_test_client():
    """
    Use a temp-file SQLite DB so that app.py's module-level create_all
    and the dependency-override session all point to the same database.
    We set DATABASE_URL via env before importing app to redirect the engine.
    """
    # Create a temp file for the test DB
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite:///{db_path}"

    # Reload database module with our test URL
    import importlib
    import src.database as db_module
    os.environ["DATABASE_URL"] = db_url
    # Re-bind the engine to our temp DB
    from sqlalchemy import create_engine as _ce
    test_engine = _ce(db_url, connect_args={"check_same_thread": False})
    db_module.engine = test_engine

    from src.database import Base, get_db
    Base.metadata.create_all(bind=test_engine)

    from sqlalchemy.orm import sessionmaker
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    from src.services import seed_demo_data
    from src.app import app

    def override_get_db():
        db = TestingSession()
        try:
            seed_demo_data(db)
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, raise_server_exceptions=True)
    return client, db_path


class TestEmployeeRoutes(unittest.TestCase):

    def setUp(self):
        from src.app import app as _app
        self.app = _app
        self.client, self.db_path = build_test_client()

    def tearDown(self):
        self.app.dependency_overrides.clear()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_list_employees(self):
        resp = self.client.get("/employees")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 3)
        names = {e["name"] for e in data}
        self.assertIn("Alice Manager", names)

    def test_get_employee_found(self):
        employees = self.client.get("/employees").json()
        bob = next(e for e in employees if e["name"] == "Bob Engineer")
        resp = self.client.get(f"/employees/{bob['id']}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("employee", data)
        self.assertIn("leave_balances", data)
        self.assertEqual(data["employee"]["name"], "Bob Engineer")

    def test_get_employee_not_found(self):
        resp = self.client.get("/employees/9999")
        self.assertEqual(resp.status_code, 404)


class TestLeaveRequestRoutes(unittest.TestCase):

    def setUp(self):
        from src.app import app as _app
        self.app = _app
        self.client, self.db_path = build_test_client()
        employees = self.client.get("/employees").json()
        self.alice = next(e for e in employees if e["name"] == "Alice Manager")
        self.bob = next(e for e in employees if e["name"] == "Bob Engineer")
        self.carol = next(e for e in employees if e["name"] == "Carol Engineer")

    def tearDown(self):
        self.app.dependency_overrides.clear()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _create_leave(self, employee_id, start=None, end=None, leave_type="annual"):
        return self.client.post("/leave-requests", json={
            "employee_id": employee_id,
            "leave_type": leave_type,
            "start_date": str(start or TOMORROW),
            "end_date": str(end or IN_3_DAYS),
        })

    def test_create_leave_request_success(self):
        resp = self._create_leave(self.bob["id"])
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["employee_id"], self.bob["id"])
        self.assertEqual(data["leave_type"], "annual")

    def test_create_leave_request_insufficient_balance(self):
        resp = self._create_leave(self.bob["id"], end=TODAY + timedelta(days=30))
        self.assertEqual(resp.status_code, 422)
        self.assertIn("Insufficient", resp.json()["detail"])

    def test_create_leave_request_backdated(self):
        resp = self._create_leave(self.bob["id"], start=TODAY - timedelta(days=1))
        self.assertEqual(resp.status_code, 422)

    def test_create_leave_request_overlap(self):
        self._create_leave(self.bob["id"], start=TOMORROW, end=IN_7_DAYS)
        resp = self._create_leave(self.bob["id"], start=IN_3_DAYS, end=IN_10_DAYS)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("overlap", resp.json()["detail"].lower())

    def test_list_leave_requests(self):
        self._create_leave(self.bob["id"])
        resp = self.client.get("/leave-requests")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("items", data)
        self.assertIn("total", data)
        self.assertGreaterEqual(data["total"], 1)

    def test_list_leave_requests_filter_by_employee(self):
        self._create_leave(self.bob["id"])
        self._create_leave(self.carol["id"], start=IN_7_DAYS, end=IN_10_DAYS)
        resp = self.client.get(f"/leave-requests?employee_id={self.bob['id']}")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertTrue(all(i["employee_id"] == self.bob["id"] for i in items))

    def test_list_leave_requests_filter_by_status(self):
        self._create_leave(self.bob["id"])
        resp = self.client.get("/leave-requests?status=pending")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertTrue(all(i["status"] == "pending" for i in items))

    def test_list_leave_requests_pagination(self):
        self._create_leave(self.bob["id"], start=TOMORROW, end=TOMORROW + timedelta(days=1))
        self._create_leave(self.bob["id"], start=IN_7_DAYS, end=IN_7_DAYS + timedelta(days=1))
        resp = self.client.get(f"/leave-requests?employee_id={self.bob['id']}&page=1&page_size=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["total"], 2)

    def test_get_leave_request_by_id(self):
        created = self._create_leave(self.bob["id"]).json()
        resp = self.client.get(f"/leave-requests/{created['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], created["id"])

    def test_get_leave_request_not_found(self):
        resp = self.client.get("/leave-requests/9999")
        self.assertEqual(resp.status_code, 404)

    def test_review_approve_leave_request(self):
        lr = self._create_leave(self.bob["id"]).json()
        resp = self.client.post(f"/leave-requests/{lr['id']}/review", json={
            "approver_id": self.alice["id"],
            "decision": "approved",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "approved")
        self.assertEqual(resp.json()["approved_by"], self.alice["id"])

    def test_review_reject_leave_request(self):
        lr = self._create_leave(self.bob["id"]).json()
        resp = self.client.post(f"/leave-requests/{lr['id']}/review", json={
            "approver_id": self.alice["id"],
            "decision": "rejected",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "rejected")

    def test_review_self_approval_rejected(self):
        lr = self._create_leave(self.bob["id"]).json()
        resp = self.client.post(f"/leave-requests/{lr['id']}/review", json={
            "approver_id": self.bob["id"],
            "decision": "approved",
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("own", resp.json()["detail"].lower())

    def test_review_non_manager_rejected(self):
        lr = self._create_leave(self.bob["id"]).json()
        resp = self.client.post(f"/leave-requests/{lr['id']}/review", json={
            "approver_id": self.carol["id"],
            "decision": "approved",
        })
        self.assertEqual(resp.status_code, 422)

    def test_cancel_pending_leave(self):
        lr = self._create_leave(self.bob["id"]).json()
        resp = self.client.post(
            f"/leave-requests/{lr['id']}/cancel",
            params={"employee_id": self.bob["id"]},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "cancelled")

    def test_cancel_approved_leave(self):
        lr = self._create_leave(self.bob["id"]).json()
        self.client.post(f"/leave-requests/{lr['id']}/review", json={
            "approver_id": self.alice["id"],
            "decision": "approved",
        })
        resp = self.client.post(
            f"/leave-requests/{lr['id']}/cancel",
            params={"employee_id": self.bob["id"]},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "cancelled")

    def test_cancel_by_non_owner_rejected(self):
        lr = self._create_leave(self.bob["id"]).json()
        resp = self.client.post(
            f"/leave-requests/{lr['id']}/cancel",
            params={"employee_id": self.carol["id"]},
        )
        self.assertEqual(resp.status_code, 422)

    def test_cancel_already_cancelled_rejected(self):
        lr = self._create_leave(self.bob["id"]).json()
        self.client.post(f"/leave-requests/{lr['id']}/cancel", params={"employee_id": self.bob["id"]})
        resp = self.client.post(f"/leave-requests/{lr['id']}/cancel", params={"employee_id": self.bob["id"]})
        self.assertEqual(resp.status_code, 422)


class TestLeaveBalanceRoutes(unittest.TestCase):

    def setUp(self):
        from src.app import app as _app
        self.app = _app
        self.client, self.db_path = build_test_client()
        employees = self.client.get("/employees").json()
        self.bob = next(e for e in employees if e["name"] == "Bob Engineer")
        self.alice = next(e for e in employees if e["name"] == "Alice Manager")

    def tearDown(self):
        self.app.dependency_overrides.clear()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_get_balances(self):
        resp = self.client.get(f"/leave-balances/{self.bob['id']}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 2)
        types = {b["leave_type"] for b in data}
        self.assertIn("annual", types)
        self.assertIn("sick", types)

    def test_get_balances_with_year(self):
        resp = self.client.get(f"/leave-balances/{self.bob['id']}?year={TODAY.year}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertGreater(len(data), 0)
        for b in data:
            self.assertEqual(b["year"], TODAY.year)

    def test_get_balances_approval_reduces_remaining(self):
        # Create and approve a leave
        lr_resp = self.client.post("/leave-requests", json={
            "employee_id": self.bob["id"],
            "leave_type": "annual",
            "start_date": str(TOMORROW),
            "end_date": str(IN_3_DAYS),
        })
        lr = lr_resp.json()
        self.client.post(f"/leave-requests/{lr['id']}/review", json={
            "approver_id": self.alice["id"],
            "decision": "approved",
        })

        resp = self.client.get(f"/leave-balances/{self.bob['id']}")
        annual = next(b for b in resp.json() if b["leave_type"] == "annual")
        days_taken = (IN_3_DAYS - TOMORROW).days + 1
        self.assertEqual(annual["used_days"], float(days_taken))
        self.assertEqual(annual["remaining_days"], 14.0 - days_taken)


if __name__ == "__main__":
    unittest.main()
