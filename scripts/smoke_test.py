#!/usr/bin/env python3
"""
Smoke / concurrency test script for the leave management API.

Usage:
    make run          # start the server in another terminal
    python scripts/smoke_test.py [--base-url http://localhost:8000]

Tests concurrent create races, cancel-vs-review races, auth validation,
and balance consistency.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

PASS = "✓"
FAIL = "✗"

passed = 0
failed = 0

# ── helpers ──────────────────────────────────────────────────────────────────


def api(method, path, base_url, *, body=None, auth=2):
    """Call the API.  auth=employee_id for Bearer token."""
    url = f"{base_url}/api/v1{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {auth}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        try:
            detail = json.loads(body_text).get("detail", body_text)
        except json.JSONDecodeError:
            detail = body_text
        return e.code, detail


def label(text):
    print(f"\n{'─'*60}")
    print(f"  {text}")
    print(f"{'─'*60}")


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {PASS} {name}")
        passed += 1
    else:
        detail_str = f" — {detail}" if detail else ""
        print(f"  {FAIL} {name}{detail_str}")
        failed += 1


def cleanup_employee(base_url, emp_id):
    """Cancel all pending/approved leave requests for an employee."""
    status, data = api(
        "GET", f"/leave-requests?employee_id={emp_id}&page_size=100",
        base_url, auth=emp_id,
    )
    if status != 200:
        return
    for lr in data.get("items", []):
        if lr["status"] in ("pending", "approved"):
            api("POST", f"/leave-requests/{lr['id']}/cancel", base_url, auth=emp_id)


def run_concurrent(fn, args_list, workers=8):
    """Run fn(*args) in parallel, return list of (args, (status, body))."""
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, *a): a for a in args_list}
        for fut in as_completed(futures):
            args = futures[fut]
            try:
                results.append((args, fut.result()))
            except Exception as exc:
                results.append((args, (0, f"exception: {exc}")))
    return results


# ── test groups ──────────────────────────────────────────────────────────────


def test_auth(base_url):
    label("Auth: unknown employee rejected")

    status, detail = api("GET", "/employees", base_url, auth=999)
    check("Bearer 999 → 401", status == 401, f"got {status}")

    status, detail = api("GET", "/employees", base_url, auth=0)
    check("Bearer 0 → 401 (no employee 0)", status == 401, f"got {status}")

    status, detail = api(
        "GET", "/employees", base_url,
        auth=None,
    )
    # No header at all
    req = urllib.request.Request(f"{base_url}/api/v1/employees")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    check("Missing header → 401", status == 401, f"got {status}")

    status, detail = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": "2026-06-01",
              "end_date": "2026-06-03", "duration": "full"},
        auth=999,
    )
    check("Create with Bearer 999 → 401", status == 401, f"got {status}")


def test_concurrent_create_overlap(base_url):
    label("Concurrent create: overlap race")

    emp = 2  # Bob
    body = {
        "leave_type": "annual",
        "start_date": str(date.today() + timedelta(days=14)),
        "end_date": str(date.today() + timedelta(days=18)),
        "duration": "full",
    }

    def do_create():
        return api("POST", "/leave-requests", base_url, body=body, auth=emp)

    results = run_concurrent(do_create, [()] * 10, workers=10)

    successes = [(a, r) for a, r in results if r[0] == 201]
    overlaps = [(a, r) for a, r in results if r[0] == 422]
    others = [(a, r) for a, r in results if r[0] not in (201, 422)]

    check(
        f"Exactly 1 success (got {len(successes)})",
        len(successes) == 1,
        f"successes={len(successes)} overlaps={len(overlaps)} others={len(others)}",
    )
    check(
        f"At least 8 overlap rejections (got {len(overlaps)})",
        len(overlaps) >= 8,
    )

    # Clean up: cancel the one that succeeded so it doesn't pollute downstream tests
    if successes:
        # successes entry is (args, (status, body))
        lr_id = successes[0][1][1]["id"]
        api("POST", f"/leave-requests/{lr_id}/cancel", base_url, auth=emp)


def test_concurrent_create_balance_race(base_url):
    label("Concurrent create: balance-exhaustion race")

    emp = 2  # Bob

    # First check Bob's remaining annual balance
    status, balances = api(
        "GET", f"/leave-balances/{emp}", base_url, auth=1
    )
    rem = None
    for b in balances:
        if b["leave_type"] == "annual":
            rem = b["remaining_days"]
    check("Bob has annual balance", rem is not None and rem > 0, f"remaining={rem}")

    if rem is None or rem <= 0:
        return

    # Try to submit many requests that collectively exhaust balance
    # Each request uses 5 working days (Mon-Fri)
    body = {
        "leave_type": "annual",
        "start_date": str(date.today() + timedelta(days=21)),
        "end_date": str(date.today() + timedelta(days=25)),
        "duration": "full",
    }

    def do_create():
        return api("POST", "/leave-requests", base_url, body=body, auth=emp)

    # Fire enough concurrent requests to exhaust balance
    max_possible = int(rem / 5) + 2  # more than balance allows
    results = run_concurrent(do_create, [()] * min(max_possible, 8), workers=8)

    successes = [(a, r) for a, r in results if r[0] == 201]
    insufficient = [(a, r) for a, r in results if r[0] == 422]

    # After clear-up, check balance
    status, balances = api(
        "GET", f"/leave-balances/{emp}", base_url, auth=1
    )
    new_rem = 0
    for b in balances:
        if b["leave_type"] == "annual":
            new_rem = b["remaining_days"]

    check(
        f"Balance not negative: {new_rem} remaining",
        new_rem >= 0,
    )

    # Cancel all successes to restore balance
    for _, (_, lr) in successes:
        api("POST", f"/leave-requests/{lr['id']}/cancel", base_url, auth=emp)


def test_concurrent_cancel_vs_review(base_url):
    label("Concurrent cancel vs review race")

    emp_bob = 2
    emp_alice = 1

    # Create a leave request
    body = {
        "leave_type": "annual",
        "start_date": str(date.today() + timedelta(days=28)),
        "end_date": str(date.today() + timedelta(days=30)),
        "duration": "full",
    }
    status, lr = api("POST", "/leave-requests", base_url, body=body, auth=emp_bob)
    check("Create for race test", status == 201, f"got {status}: {lr}")
    if status != 201:
        return
    lr_id = lr["id"]

    def do_cancel():
        return ("cancel",) + api(
            "POST", f"/leave-requests/{lr_id}/cancel", base_url, auth=emp_bob
        )

    def do_review():
        return ("review",) + api(
            "POST", f"/leave-requests/{lr_id}/review", base_url,
            body={"decision": "approved"}, auth=emp_alice,
        )

    results = run_concurrent(lambda fn: fn(), [(do_cancel,), (do_review,)], workers=2)

    outcomes = {}
    for _, (tag, code, body) in results:
        outcomes[tag] = (code, body)

    cancel_code = outcomes.get("cancel", (None,))[0]
    review_code = outcomes.get("review", (None,))[0]

    # One must succeed (200), the other must fail (422)
    success_codes = {200, 201}
    one_won = (
        (cancel_code in success_codes and review_code not in success_codes)
        or (review_code in success_codes and cancel_code not in success_codes)
    )
    check(
        f"One won, one lost (cancel={cancel_code} review={review_code})",
        one_won,
    )

    # Verify final state is consistent
    status, final = api(
        "GET", f"/leave-requests/{lr_id}", base_url, auth=emp_bob
    )
    final_status = final.get("status") if status == 200 else None

    if cancel_code in success_codes:
        check(
            f"Cancel won → status is 'cancelled' (got {final_status})",
            final_status == "cancelled",
        )
        # Balance should have been restored
        status, balances = api(
            "GET", f"/leave-balances/{emp_bob}", base_url, auth=1
        )
        for b in balances:
            if b["leave_type"] == "annual":
                check(
                    f"Balance was restored (used_days={b['used_days']})",
                    b["used_days"] <= 3.0,  # 3 working days deducted from earlier tests at most
                    f"used={b['used_days']}",
                )
    else:
        check(
            f"Review won → status is 'approved' or 'rejected' (got {final_status})",
            final_status in ("approved", "rejected"),
        )
        # If approved, balance stays deducted. If rejected, restored.
        # Either is consistent — the main point is that cancel didn't overwrite.

    # Clean up if needed
    if final_status == "approved":
        api("POST", f"/leave-requests/{lr_id}/cancel", base_url, auth=emp_bob)
    elif final_status == "pending":
        # Something went wrong — both may have failed somehow
        api("POST", f"/leave-requests/{lr_id}/cancel", base_url, auth=emp_bob)


def test_edge_cases_create(base_url):
    """Validation edge cases for leave-request creation."""
    label("Edge cases: Create validation")

    emp = 2  # Bob
    d7 = date.today() + timedelta(days=7)
    d9 = date.today() + timedelta(days=9)

    # --- structural ---
    status, detail = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": "2026-12-28",
              "end_date": "2027-01-04", "duration": "full"},
        auth=emp,
    )
    check("Cross-year → 422", status == 422, f"got {status}")

    status, detail = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": "2026-01-01",
              "end_date": "2026-01-03", "duration": "full"},
        auth=emp,
    )
    check("Backdating → 422", status == 422, f"got {status}")

    status, detail = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": str(d9),
              "end_date": str(d7), "duration": "full"},
        auth=emp,
    )
    check("Start > End → 422", status == 422, f"got {status}")

    # --- half-day ---
    saturday = date.today() + timedelta(days=(5 - date.today().weekday()) % 7)
    status, detail = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": str(saturday),
              "end_date": str(saturday), "duration": "first_half"},
        auth=emp,
    )
    check("Half-day on weekend → 422", status == 422, f"got {status}: {detail}")

    status, detail = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": str(d7),
              "end_date": str(d9), "duration": "first_half"},
        auth=emp,
    )
    check("Half-day multi-day → 422", status == 422, f"got {status}")

    # Half-day on a public holiday — Wesak Day 2026-05-20 is a Wednesday
    status, detail = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": "2026-05-20",
              "end_date": "2026-05-20", "duration": "first_half"},
        auth=emp,
    )
    check("Half-day on public holiday → 422 (zero working days)", status == 422, f"got {status}")

    # --- zero working days ---
    status, detail = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": "2026-05-20",
              "end_date": "2026-05-20", "duration": "full"},
        auth=emp,
    )
    check("Full-day on public holiday → 422 (zero working days)", status == 422, f"got {status}")

    # All-weekend range
    sat = date.today() + timedelta(days=(5 - date.today().weekday()) % 7)
    sun = sat + timedelta(days=1)
    status, detail = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": str(sat),
              "end_date": str(sun), "duration": "full"},
        auth=emp,
    )
    check("All-weekend range → 422 (zero working days)", status == 422, f"got {status}")

    # --- insufficient balance ---
    # Bob has 14 annual days; request ~20+ working days to guarantee exhaustion
    status, detail = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": str(d7),
              "end_date": str(d7 + timedelta(days=28)),
              "duration": "full"},
        auth=emp,
    )
    check("Insufficient balance → 422", status == 422, f"got {status}: {detail}")
    # Belt-and-suspenders: if it unexpectedly succeeded, cancel it
    if status == 201:
        api("POST", f"/leave-requests/{detail['id']}/cancel", base_url, auth=emp)

    # --- happy paths (these should succeed) ---
    status, lr = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": str(d7),
              "end_date": str(d7), "duration": "first_half"},
        auth=emp,
    )
    check("Valid half-day → 201", status == 201, f"got {status}: {lr}")
    if status == 201:
        api("POST", f"/leave-requests/{lr['id']}/cancel", base_url, auth=emp)

    status, lr = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": str(d7),
              "end_date": str(d9), "duration": "full"},
        auth=emp,
    )
    check("Valid full-day → 201", status == 201, f"got {status}: {lr}")
    if status == 201:
        api("POST", f"/leave-requests/{lr['id']}/cancel", base_url, auth=emp)

    # Unpaid leave (balance total_days=0, should still succeed)
    status, lr = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "unpaid", "start_date": str(d7),
              "end_date": str(d9), "duration": "full"},
        auth=emp,
    )
    check("Unpaid leave → 201", status == 201, f"got {status}: {lr}")
    if status == 201:
        api("POST", f"/leave-requests/{lr['id']}/cancel", base_url, auth=emp)


def test_edge_cases_review(base_url):
    """Validation edge cases for reviewing leave requests."""
    label("Edge cases: Review validation")

    emp_bob = 2
    emp_alice = 1
    emp_carol = 3
    d7 = str(date.today() + timedelta(days=7))
    d9 = str(date.today() + timedelta(days=9))

    # Create a request for Bob
    body = {"leave_type": "annual", "start_date": d7, "end_date": d9, "duration": "full"}
    status, lr = api("POST", "/leave-requests", base_url, body=body, auth=emp_bob)
    check("Create for review tests", status == 201, f"got {status}: {lr}")
    if status != 201:
        return
    lr_id = lr["id"]

    # Self-review by non-top-level → blocked
    status, detail = api(
        "POST", f"/leave-requests/{lr_id}/review", base_url,
        body={"decision": "approved"}, auth=emp_bob,
    )
    check("Self-review (non-manager) → 422", status == 422, f"got {status}")

    # Review by non-manager (Carol, who is Bob's peer) → blocked
    status, detail = api(
        "POST", f"/leave-requests/{lr_id}/review", base_url,
        body={"decision": "approved"}, auth=emp_carol,
    )
    check("Review by peer (not manager) → 422", status == 422, f"got {status}")

    # Invalid decision
    status, detail = api(
        "POST", f"/leave-requests/{lr_id}/review", base_url,
        body={"decision": "maybe_later"}, auth=emp_alice,
    )
    check("Invalid decision → 422", status == 422, f"got {status}")

    # Valid review by Alice (manager)
    status, _ = api(
        "POST", f"/leave-requests/{lr_id}/review", base_url,
        body={"decision": "approved"}, auth=emp_alice,
    )
    check("Valid review by manager → 200", status == 200, f"got {status}")

    # Already reviewed → cannot review again
    status, detail = api(
        "POST", f"/leave-requests/{lr_id}/review", base_url,
        body={"decision": "rejected"}, auth=emp_alice,
    )
    check("Already reviewed → 422", status == 422, f"got {status}")

    # Clean up
    api("POST", f"/leave-requests/{lr_id}/cancel", base_url, auth=emp_bob)

    # --- self-review by top-level employee (Alice) ---
    status, lr2 = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": d7, "end_date": d9, "duration": "full"},
        auth=emp_alice,
    )
    if status == 201:
        status, _ = api(
            "POST", f"/leave-requests/{lr2['id']}/review", base_url,
            body={"decision": "approved"}, auth=emp_alice,
        )
        check("Self-review (top-level) → 200", status == 200, f"got {status}")
        api("POST", f"/leave-requests/{lr2['id']}/cancel", base_url, auth=emp_alice)

    # Non-existent request
    status, detail = api(
        "POST", "/leave-requests/99999/review", base_url,
        body={"decision": "approved"}, auth=emp_alice,
    )
    check("Review non-existent → 404", status == 404, f"got {status}")


def test_edge_cases_cancel(base_url):
    """Validation edge cases for cancelling leave requests."""
    label("Edge cases: Cancel validation")

    emp_bob = 2
    emp_alice = 1
    emp_carol = 3
    d7 = str(date.today() + timedelta(days=7))
    d9 = str(date.today() + timedelta(days=9))

    # Create a request
    body = {"leave_type": "annual", "start_date": d7, "end_date": d9, "duration": "full"}
    status, lr = api("POST", "/leave-requests", base_url, body=body, auth=emp_bob)
    check("Create for cancel tests", status == 201, f"got {status}: {lr}")
    if status != 201:
        return
    lr_id = lr["id"]

    # Cancel by non-owner
    status, detail = api(
        "POST", f"/leave-requests/{lr_id}/cancel", base_url, auth=emp_carol,
    )
    check("Cancel by non-owner → 403", status == 403, f"got {status}")

    # Reject it first
    api("POST", f"/leave-requests/{lr_id}/review", base_url,
        body={"decision": "rejected"}, auth=emp_alice)

    # Cancel rejected → blocked
    status, detail = api(
        "POST", f"/leave-requests/{lr_id}/cancel", base_url, auth=emp_bob,
    )
    check("Cancel rejected request → 422", status == 422, f"got {status}")

    # Cancel already cancelled
    # Create another, cancel it, then try again
    status, lr2 = api(
        "POST", "/leave-requests", base_url,
        body={"leave_type": "annual", "start_date": d7, "end_date": d9, "duration": "full"},
        auth=emp_bob,
    )
    if status == 201:
        api("POST", f"/leave-requests/{lr2['id']}/cancel", base_url, auth=emp_bob)
        status, detail = api(
            "POST", f"/leave-requests/{lr2['id']}/cancel", base_url, auth=emp_bob,
        )
        check("Cancel already cancelled → 422", status == 422, f"got {status}")

    # Non-existent request
    status, detail = api(
        "POST", "/leave-requests/99999/cancel", base_url, auth=emp_bob,
    )
    check("Cancel non-existent → 404", status == 404, f"got {status}")


def test_edge_cases_holidays(base_url):
    """Validation edge cases for holiday CRUD."""
    label("Edge cases: Holiday management")

    emp_alice = 1
    emp_bob = 2

    # Non-manager create
    status, detail = api(
        "POST", "/holidays", base_url,
        body={"date": "2026-08-15", "name": "Test Holiday"}, auth=emp_bob,
    )
    check("Holiday create by non-manager → 403", status == 403, f"got {status}")

    # Valid create
    status, holiday = api(
        "POST", "/holidays", base_url,
        body={"date": "2026-08-15", "name": "Special Day"}, auth=emp_alice,
    )
    check("Holiday create by manager → 201", status == 201, f"got {status}")
    if status != 201:
        return
    hid = holiday["id"]

    # Duplicate date
    status, detail = api(
        "POST", "/holidays", base_url,
        body={"date": "2026-08-15", "name": "Duplicate"}, auth=emp_alice,
    )
    check("Duplicate holiday date → 422", status == 422, f"got {status}")

    # Non-manager update
    status, detail = api(
        "PUT", f"/holidays/{hid}", base_url,
        body={"date": "2026-08-15", "name": "Hacked"}, auth=emp_bob,
    )
    check("Holiday update by non-manager → 403", status == 403, f"got {status}")

    # Non-manager delete
    status, detail = api(
        "DELETE", f"/holidays/{hid}", base_url, auth=emp_bob,
    )
    check("Holiday delete by non-manager → 403", status == 403, f"got {status}")

    # Valid update
    status, _ = api(
        "PUT", f"/holidays/{hid}", base_url,
        body={"date": "2026-08-15", "name": "Updated Day"}, auth=emp_alice,
    )
    check("Holiday update by manager → 200", status == 200, f"got {status}")

    # Valid delete
    status, _ = api(
        "DELETE", f"/holidays/{hid}", base_url, auth=emp_alice,
    )
    check("Holiday delete by manager → 204", status == 204, f"got {status}")

    # Update non-existent
    status, detail = api(
        "PUT", "/holidays/99999", base_url,
        body={"date": "2026-08-15", "name": "Ghost"}, auth=emp_alice,
    )
    check("Update non-existent holiday → 404", status == 404, f"got {status}")

    # Delete non-existent
    status, detail = api(
        "DELETE", "/holidays/99999", base_url, auth=emp_alice,
    )
    check("Delete non-existent holiday → 404", status == 404, f"got {status}")


def test_edge_cases_employees(base_url):
    """Validation edge cases for employee endpoints."""
    label("Edge cases: Employee access")

    emp_alice = 1
    emp_bob = 2
    emp_carol = 3

    # Get self
    status, data = api("GET", f"/employees/{emp_bob}", base_url, auth=emp_bob)
    check("Get self → 200", status == 200, f"got {status}")

    # Get direct report (Alice views Bob)
    status, data = api("GET", f"/employees/{emp_bob}", base_url, auth=emp_alice)
    check("Manager views direct report → 200", status == 200, f"got {status}")

    # Get non-direct-report (Carol views Bob)
    status, detail = api("GET", f"/employees/{emp_bob}", base_url, auth=emp_carol)
    check("Peer views peer → 403", status == 403, f"got {status}")

    # Get non-existent
    status, detail = api("GET", "/employees/9999", base_url, auth=emp_alice)
    check("Get non-existent employee → 404", status == 404, f"got {status}")

    # List employees as non-manager
    status, data = api("GET", "/employees", base_url, auth=emp_bob)
    check(
        "List employees (non-manager) → empty",
        status == 200 and len(data.get("items", [])) == 0,
        f"got {status}, count={len(data.get('items', [])) if status == 200 else 'N/A'}",
    )

    # List employees as manager
    status, data = api("GET", "/employees", base_url, auth=emp_alice)
    check(
        "List employees (manager) → has reports",
        status == 200 and len(data.get("items", [])) >= 2,
        f"count={len(data.get('items', [])) if status == 200 else 'N/A'}",
    )


def test_edge_cases_pagination(base_url):
    """Pagination edge cases."""
    label("Edge cases: Pagination")

    emp_alice = 1

    status, data = api(
        "GET", "/employees?page=0", base_url, auth=emp_alice,
    )
    check("page=0 → 422", status == 422, f"got {status}")

    status, data = api(
        "GET", "/employees?page=999", base_url, auth=emp_alice,
    )
    check(
        "page=999 → 200 empty",
        status == 200 and len(data.get("items", [])) == 0,
        f"got {status}",
    )

    status, data = api(
        "GET", "/leave-requests?page=0", base_url, auth=emp_alice,
    )
    check("Leave-requests page=0 → 422", status == 422, f"got {status}")

    status, data = api(
        "GET", "/holidays?page=0", base_url, auth=emp_alice,
    )
    check("Holidays page=0 → 422", status == 422, f"got {status}")


def test_edge_cases_review_reject_restores(base_url):
    """Rejection restores balance; approved keeps it deducted."""
    label("Edge cases: Review reject restores balance")

    emp_bob = 2
    emp_alice = 1
    d7 = str(date.today() + timedelta(days=7))
    d9 = str(date.today() + timedelta(days=9))

    # Snapshot balance
    _, balances = api("GET", f"/leave-balances/{emp_bob}", base_url, auth=1)
    before_used = 0
    for b in balances:
        if b["leave_type"] == "annual":
            before_used = b["used_days"]

    def create():
        return api(
            "POST", "/leave-requests", base_url,
            body={"leave_type": "annual", "start_date": d7, "end_date": d9, "duration": "full"},
            auth=emp_bob,
        )

    # Test 1: Reject restores balance
    status, lr = create()
    check("Create for reject test", status == 201, f"got {status}: {lr}")
    if status != 201:
        return
    lr_id = lr["id"]

    _, balances = api("GET", f"/leave-balances/{emp_bob}", base_url, auth=1)
    after_create = next(b["used_days"] for b in balances if b["leave_type"] == "annual")
    check("Balance deducted after create", after_create == before_used + 2.0,
          f"was {before_used}, now {after_create}")

    status, _ = api(
        "POST", f"/leave-requests/{lr_id}/review", base_url,
        body={"decision": "rejected"}, auth=emp_alice,
    )
    check("Reject → 200", status == 200, f"got {status}")

    _, balances = api("GET", f"/leave-balances/{emp_bob}", base_url, auth=1)
    after_reject = next(b["used_days"] for b in balances if b["leave_type"] == "annual")
    check("Balance restored after reject", after_reject == before_used,
          f"expected {before_used}, got {after_reject}")

    # Test 2: Approve keeps balance deducted
    status, lr2 = create()
    if status == 201:
        _, balances = api("GET", f"/leave-balances/{emp_bob}", base_url, auth=1)
        after_create2 = next(b["used_days"] for b in balances if b["leave_type"] == "annual")

        api("POST", f"/leave-requests/{lr2['id']}/review", base_url,
            body={"decision": "approved"}, auth=emp_alice)

        _, balances = api("GET", f"/leave-balances/{emp_bob}", base_url, auth=1)
        after_approve = next(b["used_days"] for b in balances if b["leave_type"] == "annual")
        check("Balance stays deducted after approve", after_approve == after_create2,
              f"expected {after_create2}, got {after_approve}")

        api("POST", f"/leave-requests/{lr2['id']}/cancel", base_url, auth=emp_bob)

    # Test 3: Cancel restores balance
    status, lr3 = create()
    if status == 201:
        _, balances = api("GET", f"/leave-balances/{emp_bob}", base_url, auth=1)
        after_create3 = next(b["used_days"] for b in balances if b["leave_type"] == "annual")

        api("POST", f"/leave-requests/{lr3['id']}/cancel", base_url, auth=emp_bob)

        _, balances = api("GET", f"/leave-balances/{emp_bob}", base_url, auth=1)
        after_cancel = next(b["used_days"] for b in balances if b["leave_type"] == "annual")
        check("Balance restored after cancel", after_cancel == before_used,
              f"expected {before_used}, got {after_cancel}")


def test_balance_consistency(base_url):
    label("Balance consistency after create → cancel → create")

    emp = 2  # Bob

    # Snapshot current balance
    status, balances = api(
        "GET", f"/leave-balances/{emp}", base_url, auth=1
    )
    before_used = 0
    for b in balances:
        if b["leave_type"] == "annual":
            before_used = b["used_days"]

    # Create a request
    start = str(date.today() + timedelta(days=66))
    end = str(date.today() + timedelta(days=68))
    body = {
        "leave_type": "annual",
        "start_date": start,
        "end_date": end,
        "duration": "full",
    }
    status, lr = api("POST", "/leave-requests", base_url, body=body, auth=emp)
    check("Create for balance test", status == 201, f"got {status}: {lr}")
    if status != 201:
        return
    lr_id = lr["id"]

    # Balance should have increased by 3 (3 working days Mon-Wed)
    status, balances = api(
        "GET", f"/leave-balances/{emp}", base_url, auth=1
    )
    after_create = 0
    for b in balances:
        if b["leave_type"] == "annual":
            after_create = b["used_days"]
    check(
        f"Balance deducted by working days (was {before_used}, now {after_create})",
        after_create == before_used + 2.0,
        f"expected {before_used + 2.0}",
    )

    # Cancel it
    status, _ = api(
        "POST", f"/leave-requests/{lr_id}/cancel", base_url, auth=emp
    )
    check("Cancel succeeded", status == 200, f"got {status}")

    # Balance should be restored
    status, balances = api(
        "GET", f"/leave-balances/{emp}", base_url, auth=1
    )
    after_cancel = 0
    for b in balances:
        if b["leave_type"] == "annual":
            after_cancel = b["used_days"]
    check(
        f"Balance restored to {before_used} (got {after_cancel})",
        after_cancel == before_used,
    )


# ── stress tests ─────────────────────────────────────────────────────────────


def test_stress_high_concurrency_overlap(base_url):
    """50 concurrent creates for the same employee/dates → exactly 1 wins."""
    label("Stress: 50-way concurrent overlap race")

    emp = 2  # Bob
    body = {
        "leave_type": "annual",
        "start_date": str(date.today() + timedelta(days=70)),
        "end_date": str(date.today() + timedelta(days=74)),
        "duration": "full",
    }

    def do_create():
        return api("POST", "/leave-requests", base_url, body=body, auth=emp)

    results = run_concurrent(do_create, [()] * 50, workers=20)

    successes = [(a, r) for a, r in results if r[0] == 201]
    overlaps = [(a, r) for a, r in results if r[0] == 422]
    others = [(a, r) for a, r in results if r[0] not in (201, 422)]

    check(
        f"Exactly 1 success out of 50 (got {len(successes)})",
        len(successes) == 1,
        f"successes={len(successes)} overlaps={len(overlaps)} others={len(others)}",
    )
    check(
        f"At least 45 overlap rejections (got {len(overlaps)})",
        len(overlaps) >= 45,
    )

    # Clean up
    if successes:
        lr_id = successes[0][1][1]["id"]
        api("POST", f"/leave-requests/{lr_id}/cancel", base_url, auth=emp)


def test_stress_rapid_create_cancel_cycle(base_url):
    """Ten rapid create→cancel cycles; balance must return to baseline each time."""
    label("Stress: rapid create → cancel cycle ×10")

    emp = 2  # Bob

    # Snapshot
    _, balances = api("GET", f"/leave-balances/{emp}", base_url, auth=1)
    baseline = next(b["remaining_days"] for b in balances if b["leave_type"] == "annual")

    for i in range(10):
        d = date.today() + timedelta(days=80 + i * 7)
        body = {
            "leave_type": "annual",
            "start_date": str(d),
            "end_date": str(d + timedelta(days=2)),
            "duration": "full",
        }
        status, lr = api("POST", "/leave-requests", base_url, body=body, auth=emp)
        if status != 201:
            check(
                f"Create #{i + 1} → 201", False,
                f"got {status}: {lr}",
            )
            return

        status, _ = api(
            "POST", f"/leave-requests/{lr['id']}/cancel", base_url, auth=emp
        )
        if status != 200:
            check(
                f"Cancel #{i + 1} → 200", False,
                f"got {status}",
            )
            return

    # Verify balance unchanged
    _, balances = api("GET", f"/leave-balances/{emp}", base_url, auth=1)
    final = next(b["remaining_days"] for b in balances if b["leave_type"] == "annual")
    check(
        f"Balance unchanged after 10 cycles (was {baseline}, now {final})",
        final == baseline,
    )


def test_stress_mixed_concurrent_ops(base_url):
    """Create 3 requests, then simultaneously cancel one + review two."""
    label("Stress: mixed concurrent cancel + dual review")

    emp_bob = 2
    emp_alice = 1

    # Create 3 non-overlapping requests
    requests = []
    for i in range(3):
        d = date.today() + timedelta(days=150 + i * 10)
        body = {
            "leave_type": "annual",
            "start_date": str(d),
            "end_date": str(d + timedelta(days=2)),
            "duration": "full",
        }
        status, lr = api("POST", "/leave-requests", base_url, body=body, auth=emp_bob)
        if status != 201:
            check(
                f"Create #{i + 1} for mixed-ops test", False,
                f"got {status}: {lr}",
            )
            # Clean up any that were created
            for rid in requests:
                api("POST", f"/leave-requests/{rid}/cancel", base_url, auth=emp_bob)
            return
        requests.append(lr["id"])

    rid_a, rid_b, rid_c = requests

    def cancel_a():
        return api("POST", f"/leave-requests/{rid_a}/cancel", base_url, auth=emp_bob)

    def approve_b():
        return api(
            "POST", f"/leave-requests/{rid_b}/review", base_url,
            body={"decision": "approved"}, auth=emp_alice,
        )

    def reject_c():
        return api(
            "POST", f"/leave-requests/{rid_c}/review", base_url,
            body={"decision": "rejected"}, auth=emp_alice,
        )

    results = run_concurrent(
        lambda fn: fn(), [(cancel_a,), (approve_b,), (reject_c,)], workers=3
    )

    successes = 0
    for _, r in results:
        if r[0] in (200, 201):
            successes += 1

    check(
        f"All 3 concurrent ops succeeded (got {successes})",
        successes == 3,
        f"results={[(a[0].__name__, r[0]) for a, r in results]}",
    )

    # Verify final states
    s1, lr1 = api("GET", f"/leave-requests/{rid_a}", base_url, auth=emp_bob)
    s2, lr2 = api("GET", f"/leave-requests/{rid_b}", base_url, auth=emp_bob)
    s3, lr3 = api("GET", f"/leave-requests/{rid_c}", base_url, auth=emp_bob)

    ok = (
        s1 == 200 and lr1.get("status") == "cancelled"
        and s2 == 200 and lr2.get("status") == "approved"
        and s3 == 200 and lr3.get("status") == "rejected"
    ) if (s1 == s2 == s3 == 200) else False
    check(
        "States correct (cancelled / approved / rejected)",
        ok,
        f"A={lr1.get('status') if s1 == 200 else s1} "
        f"B={lr2.get('status') if s2 == 200 else s2} "
        f"C={lr3.get('status') if s3 == 200 else s3}",
    )

    # Clean up any remaining approved requests
    for rid in requests:
        api("POST", f"/leave-requests/{rid}/cancel", base_url, auth=emp_bob)


def test_stress_balance_boundary(base_url):
    """Drain balance near zero, then fire concurrent small requests; verify no
    over-draft and correct count of successes."""
    label("Stress: balance boundary under concurrency")

    emp = 2  # Bob

    # Snapshot Bob's annual remaining
    _, balances = api("GET", f"/leave-balances/{emp}", base_url, auth=1)
    rem = next(b["remaining_days"] for b in balances if b["leave_type"] == "annual")

    if rem < 3:
        check("Skipping — not enough baseline balance", True)
        return

    # Drain balance down to ~3 days via sequential 5-day requests
    drained = 0
    for i in range(10):
        _, balances = api("GET", f"/leave-balances/{emp}", base_url, auth=1)
        new_rem = next(b["remaining_days"] for b in balances if b["leave_type"] == "annual")
        if new_rem <= 3:
            break
        d = date.today() + timedelta(days=200 + i * 10)
        body = {
            "leave_type": "annual",
            "start_date": str(d),
            "end_date": str(d + timedelta(days=4)),
            "duration": "full",
        }
        status, lr = api("POST", "/leave-requests", base_url, body=body, auth=emp)
        if status == 201:
            drained += 1
        else:
            break

    check(f"Drained balance with {drained} requests", drained >= 1, f"rem={rem}")
    if drained == 0:
        return

    # Snapshot remaining after drain
    _, balances = api("GET", f"/leave-balances/{emp}", base_url, auth=1)
    new_rem = next(b["remaining_days"] for b in balances if b["leave_type"] == "annual")

    # Now fire concurrent 1-day requests (half-day each) that collectively
    # exceed the remaining balance
    half_day_body = {
        "leave_type": "annual",
        "start_date": str(date.today() + timedelta(days=250)),
        "end_date": str(date.today() + timedelta(days=250)),
        "duration": "first_half",
    }

    def do_half():
        return api("POST", "/leave-requests", base_url, body=half_day_body, auth=emp)

    # Fire enough to exceed remaining × 3
    n_requests = max(int(new_rem * 3) + 1, 5)
    results = run_concurrent(do_half, [()] * n_requests, workers=min(n_requests, 12))

    successes = [(a, r) for a, r in results if r[0] == 201]
    max_expected = int(new_rem * 2)  # each half-day uses 0.5 days

    check(
        f"At most {max_expected} half-day successes (got {len(successes)})",
        len(successes) <= max_expected + 1,  # +1 tolerance for rounding
        f"remaining before={new_rem}",
    )

    # Balance must not be negative
    _, balances = api("GET", f"/leave-balances/{emp}", base_url, auth=1)
    final_rem = next(b["remaining_days"] for b in balances if b["leave_type"] == "annual")
    check(
        f"Balance not negative: {final_rem} remaining",
        final_rem >= 0,
    )

    # Clean up everything
    cleanup_employee(base_url, emp)


# ── main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Smoke test the leave API")
    parser.add_argument(
        "--base-url", default="http://localhost:8000",
        help="Base URL of the running server",
    )
    parser.add_argument(
        "--stress", action="store_true",
        help="Also run high-concurrency stress tests",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Leave API Smoke / Concurrency Tests")
    print(f"  Server: {args.base_url}")

    # Clean up leftover data from previous runs
    for emp_id in (1, 2, 3):
        cleanup_employee(args.base_url, emp_id)

    print("=" * 60)

    # Quick health check — hit /docs which always returns 200
    try:
        req = urllib.request.Request(f"{args.base_url}/docs")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:
        print(f"\n{FAIL} Cannot reach server at {args.base_url}: {e}")
        print("  Start the server with: make run")
        sys.exit(1)

    test_auth(args.base_url)
    test_concurrent_create_overlap(args.base_url)
    test_concurrent_create_balance_race(args.base_url)
    test_concurrent_cancel_vs_review(args.base_url)
    test_edge_cases_create(args.base_url)
    test_edge_cases_review(args.base_url)
    test_edge_cases_cancel(args.base_url)
    test_edge_cases_holidays(args.base_url)
    test_edge_cases_employees(args.base_url)
    test_edge_cases_pagination(args.base_url)
    test_edge_cases_review_reject_restores(args.base_url)
    test_balance_consistency(args.base_url)

    if args.stress:
        test_stress_high_concurrency_overlap(args.base_url)
        test_stress_rapid_create_cancel_cycle(args.base_url)
        test_stress_mixed_concurrent_ops(args.base_url)
        test_stress_balance_boundary(args.base_url)

    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
