"""
Integration tests for the FastAPI leave management API.

These tests use TestClient with an in-memory SQLite database to exercise
the full stack in isolation. Each test gets a fresh seeded database.
"""

import pytest
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.app import app
from src import services
from src.models import Employee, LeaveType, LeaveStatus, LeaveRequest, LeaveBalance


@pytest.fixture(scope="function")
def client_and_db():
    # StaticPool ensures all connections share the same in-memory SQLite database
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(bind=engine)

    db_session = SessionFactory()
    services.seed_demo_data(db_session)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client, db_session
    db_session.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ── Helpers ───────────────────────────────────────────────────────────────

def _future_date(days: int) -> str:
    return str(date.today() + timedelta(days=days))


# ── POST /leave-requests ──────────────────────────────────────────────────

def test_create_leave_request_happy_path(client_and_db):
    """Bob submits a valid annual leave request for future dates."""
    client, db = client_and_db
    bob = db.query(Employee).filter(Employee.email == "bob@company.com").first()

    resp = client.post("/leave-requests", json={
        "employee_id": bob.id,
        "leave_type": "annual",
        "start_date": _future_date(7),
        "end_date": _future_date(9),
        "reason": "Vacation",
    })

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["employee_id"] == bob.id
    assert body["leave_type"] == "annual"


def test_create_leave_request_invalid_date_range(client_and_db):
    """Submitting start_date > end_date returns 422 with code INVALID_DATE_RANGE."""
    client, db = client_and_db
    bob = db.query(Employee).filter(Employee.email == "bob@company.com").first()

    resp = client.post("/leave-requests", json={
        "employee_id": bob.id,
        "leave_type": "annual",
        "start_date": _future_date(10),
        "end_date": _future_date(7),
    })

    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["code"] == "INVALID_DATE_RANGE"


# ── POST /leave-requests/{id}/review ─────────────────────────────────────

def test_review_leave_request_approval_happy_path(client_and_db):
    """Alice approves Bob's pending leave request."""
    client, db = client_and_db
    alice = db.query(Employee).filter(Employee.email == "alice@company.com").first()
    bob = db.query(Employee).filter(Employee.email == "bob@company.com").first()

    # Bob submits a leave request
    create_resp = client.post("/leave-requests", json={
        "employee_id": bob.id,
        "leave_type": "annual",
        "start_date": _future_date(7),
        "end_date": _future_date(9),
    })
    assert create_resp.status_code == 201
    leave_id = create_resp.json()["id"]

    # Alice approves it
    resp = client.post(f"/leave-requests/{leave_id}/review", json={
        "decision": "approved",
        "approver_id": alice.id,
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == alice.id


def test_review_leave_request_self_approval(client_and_db):
    """Bob cannot approve his own leave request — returns 403 with SELF_APPROVAL."""
    client, db = client_and_db
    bob = db.query(Employee).filter(Employee.email == "bob@company.com").first()

    # Bob submits a leave request
    create_resp = client.post("/leave-requests", json={
        "employee_id": bob.id,
        "leave_type": "annual",
        "start_date": _future_date(7),
        "end_date": _future_date(9),
    })
    assert create_resp.status_code == 201
    leave_id = create_resp.json()["id"]

    # Bob tries to approve his own request
    resp = client.post(f"/leave-requests/{leave_id}/review", json={
        "decision": "approved",
        "approver_id": bob.id,
    })

    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"]["code"] == "SELF_APPROVAL"


# ── POST /leave-requests/{id}/cancel ─────────────────────────────────────

def test_cancel_leave_request_happy_path(client_and_db):
    """Bob cancels his own pending leave request."""
    client, db = client_and_db
    bob = db.query(Employee).filter(Employee.email == "bob@company.com").first()

    # Bob submits a leave request
    create_resp = client.post("/leave-requests", json={
        "employee_id": bob.id,
        "leave_type": "annual",
        "start_date": _future_date(7),
        "end_date": _future_date(9),
    })
    assert create_resp.status_code == 201
    leave_id = create_resp.json()["id"]

    # Bob cancels it
    resp = client.post(f"/leave-requests/{leave_id}/cancel", params={"employee_id": bob.id})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"


def test_cancel_leave_request_non_owner(client_and_db):
    """Carol cannot cancel Bob's request — returns 403 with NOT_REQUEST_OWNER."""
    client, db = client_and_db
    bob = db.query(Employee).filter(Employee.email == "bob@company.com").first()
    carol = db.query(Employee).filter(Employee.email == "carol@company.com").first()

    # Bob submits a leave request
    create_resp = client.post("/leave-requests", json={
        "employee_id": bob.id,
        "leave_type": "annual",
        "start_date": _future_date(7),
        "end_date": _future_date(9),
    })
    assert create_resp.status_code == 201
    leave_id = create_resp.json()["id"]

    # Carol tries to cancel Bob's request
    resp = client.post(f"/leave-requests/{leave_id}/cancel", params={"employee_id": carol.id})

    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"]["code"] == "NOT_REQUEST_OWNER"


# ── GET /leave-requests ───────────────────────────────────────────────────

def test_list_leave_requests_filtering_and_pagination(client_and_db):
    """Querying with status=pending returns paginated results with correct structure."""
    client, db = client_and_db
    bob = db.query(Employee).filter(Employee.email == "bob@company.com").first()

    # Submit a few leave requests
    for i in range(3):
        start = _future_date(7 + i * 5)
        end = _future_date(9 + i * 5)
        client.post("/leave-requests", json={
            "employee_id": bob.id,
            "leave_type": "annual",
            "start_date": start,
            "end_date": end,
        })

    resp = client.get("/leave-requests", params={
        "status": "pending",
        "page": 1,
        "page_size": 5,
    })

    assert resp.status_code == 200
    body = resp.json()
    assert "total" in body
    assert "page" in body
    assert "page_size" in body
    assert "items" in body
    assert body["page"] == 1
    assert body["page_size"] == 5
    assert isinstance(body["items"], list)


# ── GET /leave-balances/{employee_id} ────────────────────────────────────

def test_get_leave_balances_returns_zero_for_missing_types(client_and_db):
    """Bob has no paternity balance — response contains paternity with remaining_days == 0."""
    client, db = client_and_db
    bob = db.query(Employee).filter(Employee.email == "bob@company.com").first()

    resp = client.get(f"/leave-balances/{bob.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)

    paternity_entries = [b for b in body if b["leave_type"] == "paternity"]
    assert len(paternity_entries) == 1
    assert paternity_entries[0]["remaining_days"] == 0
