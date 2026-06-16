# Design Document - Leave Management System

**Author:** Hafiz Iskandar
**Date:** 2026-06-01

---

## 1. API Design

### Endpoints

| Method | Path | Description | Success | Error |
|--------|------|-------------|---------|-------|
| GET | `/employees` | List all employees | 200 | - |
| GET | `/employees/{id}` | Employee detail + balances | 200 | 404 |
| POST | `/leave-requests` | Create a leave request | 201 | 422 |
| GET | `/leave-requests` | List/filter leave requests | 200 | - |
| GET | `/leave-requests/{id}` | Get a single request | 200 | 404 |
| POST | `/leave-requests/{id}/review` | Approve or reject | 200 | 422 |
| POST | `/leave-requests/{id}/cancel` | Cancel a request | 200 | 422 |
| GET | `/leave-balances/{employee_id}` | Get leave balances | 200 | - |

### Request/Response Shapes

**POST `/leave-requests`**
```json
{
  "employee_id": 2,
  "leave_type": "annual",
  "start_date": "2026-07-01",
  "end_date": "2026-07-03",
  "reason": "Holiday"
}
```
Response: `LeaveRequestOut` (id, employee_id, leave_type, start_date, end_date, status, approved_by, approved_at)

**POST `/leave-requests/{id}/review`**
```json
{
  "approver_id": 1,
  "decision": "approved"
}
```
The `approver_id` field was added to fix the original hardcoded `approver_id=1` in the scaffold.
In a real system this would come from the authenticated session instead.

**GET `/leave-requests`** - all query params are optional:
- `employee_id`, `status`, `leave_type`, `from_date`, `to_date`
- `page` (default 1), `page_size` (default 20, max 100)

Response: `{ items, total, page, page_size }` (offset-based pagination).

---

## 2. Data Model

The scaffold provides three core tables:

### `employees`
- `id`, `name`, `email` (unique), `department`
- `manager_id` → self-referential FK (nullable; top-level managers have no manager)
- `joined_at`, `created_at`, `updated_at`

### `leave_requests`
- `id`, `employee_id` (FK), `leave_type` (enum), `start_date`, `end_date`, `reason`
- `status` (enum: pending / approved / rejected / cancelled), default: pending
- `approved_by` (FK → employees), `approved_at`

### `leave_balances`
- `id`, `employee_id` (FK), `leave_type` (enum), `year`
- `total_days` (Float - supports half-days), `used_days` (Float)
- `remaining_days` is a computed property: `total_days - used_days`

**Key design notes:**
- `Float` for days rather than integer to support half-day leaves without a schema change.
- `leave_balances` is scoped per year - a new row per employee per leave type per year. This avoids year-boundary pollution.
- No unique constraint on `(employee_id, leave_type, year)` in the scaffold; my implementation relies on the query being specific enough and does not add duplicates.

---

## 3. Edge Cases Identified

### Overlapping leave requests
Detected with a SQL range intersection query:
`existing.start_date <= new.end_date AND existing.end_date >= new.start_date`
Only non-cancelled and non-rejected requests are considered active.

### Insufficient balance
Checked at create time using `remaining_days` (total − used). Re-checked at approval time because the window between creation and approval could allow the balance to drop (e.g., another request is approved in between).
If no balance row exists for that leave type/year, the request is treated as having `0` available days rather than auto-creating a zero-day balance row. This keeps failed validation side-effect free.

### Self-approval
`SelfApprovalError` raised if `approver_id == employee_id`. The approver must be the direct manager (`employee.manager_id == approver_id`).

### Cancelling an approved leave restores balance
`cancel_leave_request` checks if the leave was `APPROVED` before cancellation and reverses the `used_days` deduction. A `max(0, ...)` guard prevents underflow from any edge case.

### UNPAID leave
Never consumes a balance record. The check `leave_type != UNPAID` is applied both at create and approve time.

### Concurrent approvals (two managers simultaneously)
Handled with SQLAlchemy `with_for_update()` on the `LeaveBalance` row at approval time. This is a pessimistic lock - the second concurrent transaction will block until the first commits, then see the updated balance and raise `InsufficientBalanceError` if it's been exhausted.

For the `LeaveRequest` row itself, the status check (`status == PENDING`) acts as an optimistic guard: only one transaction can flip the status from PENDING to APPROVED; the other will see the non-PENDING state and reject.

### Back-dated leave
`start_date < today` is rejected at create time.

### Leave spanning year boundary
Not handled in this implementation (see "What I Would Do With More Time"). Currently the balance bucket is keyed to `start_date.year`, so a request spanning Dec 31 → Jan 1 would deduct entirely from the starting year's balance.

### Half-day leaves
The data model (`Float` days) supports half-days. The current `_count_working_days` counts calendar days and would need a `0.5` multiplier if a `half_day: bool` field were added to `LeaveRequest`.

### Idempotency of approval
Attempting to approve an already-approved request raises `LeaveError("already approved")`. There is no silent no-op - this is intentional so callers know their request arrived late.

---

## 4. Tradeoffs and Decisions

### SQLite for the demo
**Chosen:** SQLite (as given in the scaffold).  
**Alternative:** PostgreSQL.  
**Why:** SQLite removes setup friction for the challenge evaluator. The code is compatible with PostgreSQL - just change `DATABASE_URL`. The one caveat is that `with_for_update()` on SQLite is a no-op (SQLite uses file-level locking), so the concurrency protection only kicks in properly with Postgres in production.

### Synchronous FastAPI (not async)
**Chosen:** Sync SQLAlchemy sessions.  
**Alternative:** `asyncpg` + `SQLAlchemy async`.  
**Why:** The scaffold uses sync SQLAlchemy. Async would require changing the session management, engine, and all ORM queries. For a CRUD-heavy leave system the throughput gains are modest. I kept it sync to avoid scope creep.

### Pessimistic locking for balance deduction
**Chosen:** `SELECT ... FOR UPDATE` on `LeaveBalance`.  
**Alternative:** Optimistic locking (version column + retry on conflict).  
**Why:** Leave approvals are low-frequency, so lock contention is rare. Pessimistic locking is simpler to reason about correctly: no retry logic, no partial commit risk. Optimistic locking would be preferable at high scale or with a distributed system.

### Offset-based pagination
**Chosen:** `OFFSET / LIMIT`.  
**Alternative:** Cursor-based (keyset) pagination.  
**Why:** The dataset size for a company's leave requests is small enough that offset pagination is fine. Cursor-based pagination is more stable for large, frequently-updated datasets (avoids the "page drift" problem), but adds implementation and API complexity not warranted here.

### Balance deduction on approval, not on creation
**Chosen:** Balance deducted when a leave is approved.  
**Alternative:** Deduct on creation, refund on rejection.  
**Why:** Creation represents intent; approval represents commitment. Deducting on creation would incorrectly block employees from requesting leave while a previous request is still pending (even if it will be rejected). The current approach means an employee could have multiple pending requests that together exceed their balance - the first to be approved wins. This is acceptable and noted.

### approver_id in request body (not auth token)
**Chosen:** Pass `approver_id` as a field in `LeaveRequestApprove`.  
**Alternative:** Derive from authenticated session (JWT / session cookie).  
**Why:** There is no auth system in the scaffold. Making the approver explicit in the body is the pragmatic choice for this challenge. In production, `approver_id` would come from the auth middleware.

---

## 5. What I Would Do With More Time

**Authentication & authorization**
Real JWT-based auth so that `approver_id` comes from the token, not the request body. Role-based access control (manager, HR admin, employee).

**Weekend and public holiday exclusion**
A `working_days(start, end)` function that skips weekends and a configurable public holiday calendar (stored in DB or from a Malaysian public holiday API). This would change the balance deduction from calendar days to working days.

**Year-boundary leave handling**
When a leave spans two calendar years, split the deduction across both years' balance records proportionally.

**Async + PostgreSQL**
Move to `asyncpg` + async SQLAlchemy for better throughput under real load. Use PostgreSQL for proper `SELECT FOR UPDATE` semantics.

**Cursor-based pagination**
Replace offset with a stable keyset cursor, especially for manager dashboards that list all pending requests across a large team.

**Structured logging**
Add request-scoped logging (employee_id, action, outcome) for audit trail purposes - essential for an HR system.

**Half-day leaves**
Add `is_half_day: bool` to `LeaveRequest`. Adjust `_count_working_days` to return `0.5` for same-day half-day requests.

**Leave policy engine**
Currently leave balances are seeded manually. A policy engine would define accrual rules (e.g., 1.17 days/month for annual leave) and automatically create/top-up balance records.

**Delegation / acting manager**
When a manager is on leave, a delegate should be able to approve. This requires a `delegate_manager_id` on `Employee` and updated authorization logic.

---

## 6. Running the Project

```bash
# Install dependencies
make install

# Run the server (seeds demo data on startup)
make run

# Run tests
make test

# Or directly from the virtualenv
./.venv/bin/python -m pytest tests/ -v
```

Demo employees are seeded automatically:
- **Alice Manager** (id=1) - no manager, can approve Bob and Carol's leaves
- **Bob Engineer** (id=2) - manager: Alice, 14 annual + 12 sick days
- **Carol Engineer** (id=3) - manager: Alice, 14 annual + 12 sick days
