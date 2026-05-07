"""HTTP-level integration tests via TestClient."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(seed):
    # Import inside the fixture so DATABASE_URL from conftest is in effect.
    from src.app import app
    return TestClient(app)


def _next_monday() -> date:
    d = date.today()
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_employees(client, seed):
    resp = client.get("/employees")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_get_employee_with_balances(client, seed):
    bob = seed["bob"]
    resp = client.get(f"/employees/{bob.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["employee"]["email"] == "bob@x.com"
    assert any(b["leave_type"] == "annual" for b in body["leave_balances"])


def test_create_leave_request_requires_auth_header(client, seed):
    bob = seed["bob"]
    start = _next_monday()
    resp = client.post("/leave-requests", json={
        "employee_id": bob.id,
        "leave_type": "annual",
        "start_date": str(start),
        "end_date": str(start),
    })
    assert resp.status_code == 401


def test_create_leave_request_happy(client, seed):
    bob = seed["bob"]
    start = _next_monday()
    resp = client.post(
        "/leave-requests",
        headers={"X-Employee-Id": str(bob.id)},
        json={
            "employee_id": bob.id,
            "leave_type": "annual",
            "start_date": str(start),
            "end_date": str(start + timedelta(days=2)),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["working_days"] == 3.0
    assert "X-Request-ID" in resp.headers


def test_create_leave_request_cannot_act_for_others(client, seed):
    bob, carol = seed["bob"], seed["carol"]
    start = _next_monday()
    resp = client.post(
        "/leave-requests",
        headers={"X-Employee-Id": str(carol.id)},
        json={
            "employee_id": bob.id,
            "leave_type": "annual",
            "start_date": str(start),
            "end_date": str(start),
        },
    )
    assert resp.status_code == 403


def test_review_full_flow(client, seed):
    alice, bob = seed["alice"], seed["bob"]
    start = _next_monday()
    create = client.post(
        "/leave-requests",
        headers={"X-Employee-Id": str(bob.id)},
        json={
            "employee_id": bob.id,
            "leave_type": "annual",
            "start_date": str(start),
            "end_date": str(start),
        },
    )
    assert create.status_code == 201
    lr_id = create.json()["id"]

    # Bob (self) cannot approve
    self_review = client.post(
        f"/leave-requests/{lr_id}/review",
        headers={"X-Employee-Id": str(bob.id)},
        json={"decision": "approved"},
    )
    assert self_review.status_code == 403

    # Alice (manager) approves
    review = client.post(
        f"/leave-requests/{lr_id}/review",
        headers={"X-Employee-Id": str(alice.id)},
        json={"decision": "approved"},
    )
    assert review.status_code == 200, review.text
    assert review.json()["status"] == "approved"


def test_list_leave_requests_pagination(client, seed):
    bob = seed["bob"]
    start = _next_monday()
    for week in range(3):
        client.post(
            "/leave-requests",
            headers={"X-Employee-Id": str(bob.id)},
            json={
                "employee_id": bob.id,
                "leave_type": "annual",
                "start_date": str(start + timedelta(days=7 * week)),
                "end_date": str(start + timedelta(days=7 * week)),
            },
        ).raise_for_status()

    resp = client.get(
        "/leave-requests",
        params={"employee_id": bob.id, "page_size": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_cancel_endpoint(client, seed):
    bob = seed["bob"]
    start = _next_monday()
    create = client.post(
        "/leave-requests",
        headers={"X-Employee-Id": str(bob.id)},
        json={
            "employee_id": bob.id,
            "leave_type": "annual",
            "start_date": str(start),
            "end_date": str(start),
        },
    )
    lr_id = create.json()["id"]
    cancel = client.post(
        f"/leave-requests/{lr_id}/cancel",
        headers={"X-Employee-Id": str(bob.id)},
    )
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"
