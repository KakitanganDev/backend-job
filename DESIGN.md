# Design Document — Leave Management System

**Author:** Ahmad Dzafran Mohamad Bustaman
**Date:** 2026-05-29

## 1. API Design

All error responses follow the shape `{"detail": "<human-readable>", "code": "<ERROR_CODE>"}`.
The `code` field is a screaming-snake-case string (e.g., `INSUFFICIENT_BALANCE`) used by
callers to branch on error type without parsing the human-readable message.

---

### `POST /leave-requests`

**Request body:**

```json
{
  "employee_id": 2,
  "leave_type": "annual",
  "start_date": "2026-06-01",
  "end_date": "2026-06-03",
  "half_day_start": false,
  "half_day_end": false,
  "reason": "Family trip to Penang"
}
```

**Response (201):**

```json
{
  "id": 42,
  "employee_id": 2,
  "leave_type": "annual",
  "start_date": "2026-06-01",
  "end_date": "2026-06-03",
  "half_day_start": false,
  "half_day_end": false,
  "reason": "Family trip to Penang",
  "status": "pending",
  "approved_by": null,
  "approved_at": null,
  "estimated_deductions": [{ "year": 2026, "days": 3.0 }]
}
```

The `estimated_deductions` array shows what will come off the balance on approval, broken
out by calendar year. For year-spanning requests it will contain two entries. The balance
itself does not change at this point; the estimate is informational.

**Error codes:**

| Status | `code`                 | Condition                                                |
| ------ | ---------------------- | -------------------------------------------------------- |
| 404    | `EMPLOYEE_NOT_FOUND`   | `employee_id` does not match any employee                |
| 422    | `INVALID_DATE_RANGE`   | `end_date < start_date`                                  |
| 422    | `BACKDATED_REQUEST`    | `start_date < today`                                     |
| 422    | `UNKNOWN_LEAVE_TYPE`   | `leave_type` is not in the `LeaveType` enum              |
| 422    | `NO_WORKING_DAYS`      | The range contains no working days (all weekend/holiday) |
| 422    | `OVERLAPPING_LEAVE`    | Overlaps an existing pending or approved request         |
| 422    | `INSUFFICIENT_BALANCE` | Not enough balance; message names the year and counts    |

Validation runs cheapest-first: date sanity → leave type → working-day count > 0 →
own-leaves overlap → sufficient balance. The first failure stops evaluation.

---

### `POST /leave-requests/{id}/review`

**Request body:**

```json
{
  "approver_id": 1,
  "decision": "approved"
}
```

`decision` may be `"approved"` or `"rejected"`.

**Response (200):**

```json
{
  "id": 42,
  "employee_id": 2,
  "leave_type": "annual",
  "status": "approved",
  "approved_by": 1,
  "approved_at": "2026-05-30T09:15:42Z",
  "deductions": [{ "year": 2026, "days": 3.0 }]
}
```

`deductions` is present only when `decision == "approved"`. It mirrors the per-year split
that was written to `leave_deductions` and applied to `leave_balances.used_days`.

For a rejection response, `deductions` is absent and no balance row is touched.

**Error codes:**

| Status | `code`                    | Condition                                                       |
| ------ | ------------------------- | --------------------------------------------------------------- |
| 404    | `LEAVE_REQUEST_NOT_FOUND` | `id` does not match any request                                 |
| 403    | `NOT_AUTHORIZED_APPROVER` | `approver_id` is not the employee's recorded manager            |
| 403    | `SELF_APPROVAL`           | `approver_id == employee_id`                                    |
| 409    | `REQUEST_NOT_PENDING`     | The request is no longer pending (race lost / already terminal) |
| 422    | `INSUFFICIENT_BALANCE`    | Balance dropped between submission and this approval            |

---

### `POST /leave-requests/{id}/cancel`

**Request:** `?employee_id=2` (query parameter).

**Response (200):**

```json
{
  "id": 42,
  "status": "cancelled",
  "restored_deductions": [{ "year": 2026, "days": 3.0 }]
}
```

`restored_deductions` lists what was added back to `leave_balances.used_days` for each
calendar year. For a cancellation of a pending request, the array is empty (no deduction
was ever recorded).

**Error codes:**

| Status | `code`                     | Condition                                   |
| ------ | -------------------------- | ------------------------------------------- |
| 404    | `LEAVE_REQUEST_NOT_FOUND`  | `id` does not match any request             |
| 403    | `NOT_REQUEST_OWNER`        | `employee_id` is not the request's owner    |
| 409    | `ALREADY_CANCELLED`        | Status is already `cancelled`               |
| 409    | `REJECTED_NOT_CANCELLABLE` | Status is `rejected`; rejection is terminal |

---

### `GET /leave-requests`

Query parameters: `employee_id`, `status`, `leave_type`, `from_date`, `to_date`,
`page` (default 1, min 1), `page_size` (default 20, min 1, max 100).

**Response (200):**

```json
{
  "items": [...],
  "total": 35,
  "page": 2,
  "page_size": 10
}
```

`total` is the count of all matching rows, not the length of `items`. Callers use it to
compute total pages without issuing a second request.

---

### `GET /employees` / `GET /employees/{id}`

`GET /employees` returns a flat list of all employees. `GET /employees/{id}` returns the
employee record together with their current-year leave balances. Returns 404 if the
employee does not exist.

---

### `GET /leave-balances/{employee_id}`

Optional query parameter `year` (defaults to `date.today().year`). Returns one entry per
`LeaveType` value. If no balance row exists for a given type, the endpoint synthesizes a
zero row (`total_days=0, used_days=0, remaining_days=0`) rather than omitting the entry or
returning an error.

---

## 2. Data Model

### `employees`

| Column       | Type     | Constraints                         |
| ------------ | -------- | ----------------------------------- |
| `id`         | Integer  | PK, autoincrement, indexed          |
| `name`       | String   | NOT NULL                            |
| `email`      | String   | NOT NULL, UNIQUE, indexed           |
| `department` | String   | NOT NULL                            |
| `manager_id` | Integer  | FK → `employees.id`, nullable       |
| `joined_at`  | Date     | default `date.today`                |
| `created_at` | DateTime | default `utcnow`                    |
| `updated_at` | DateTime | default `utcnow`, `onupdate=utcnow` |

Self-referential FK on `manager_id`. The manager relationship is the only
approval-authority signal in this release. Alice is her own reporting root
(`manager_id` is null).

---

### `leave_requests`

| Column           | Type          | Constraints                            |
| ---------------- | ------------- | -------------------------------------- |
| `id`             | Integer       | PK, autoincrement, indexed             |
| `employee_id`    | Integer       | FK → `employees.id`, NOT NULL, indexed |
| `leave_type`     | `LeaveType`   | NOT NULL                               |
| `start_date`     | Date          | NOT NULL                               |
| `end_date`       | Date          | NOT NULL                               |
| `half_day_start` | Boolean       | NOT NULL, default `false`              |
| `half_day_end`   | Boolean       | NOT NULL, default `false`              |
| `reason`         | String        | nullable, max 500 chars                |
| `status`         | `LeaveStatus` | NOT NULL, default `pending`            |
| `approved_by`    | Integer       | FK → `employees.id`, nullable          |
| `approved_at`    | DateTime      | nullable                               |
| `created_at`     | DateTime      | default `utcnow`                       |
| `updated_at`     | DateTime      | default `utcnow`, `onupdate=utcnow`    |

`half_day_start` and `half_day_end` are independent markers. For same-day
requests (`start_date == end_date`), only `half_day_start` is honored.
For multi-day requests, `half_day_start` applies to the first day only and
`half_day_end` to the last day only.

The `status` column is the target of every compare-and-swap update.

---

### `leave_balances`

| Column        | Type        | Constraints                            |
| ------------- | ----------- | -------------------------------------- |
| `id`          | Integer     | PK, autoincrement                      |
| `employee_id` | Integer     | FK → `employees.id`, NOT NULL, indexed |
| `leave_type`  | `LeaveType` | NOT NULL                               |
| `year`        | Integer     | NOT NULL                               |
| `total_days`  | Float       | NOT NULL, default `0`                  |
| `used_days`   | Float       | NOT NULL, default `0`                  |
| `created_at`  | DateTime    | default `utcnow`                       |
| `updated_at`  | DateTime    | default `utcnow`, `onupdate=utcnow`    |

`remaining_days` is a Python property (`total_days - used_days`), not stored. This
removes one class of drift: only `used_days` is ever written by the service; the math
never has two separate columns to keep in sync.

There is no explicit UNIQUE constraint on `(employee_id, leave_type, year)` in the
baseline model; in production, one should be added.

---

### `holidays`

| Column       | Type     | Constraints       |
| ------------ | -------- | ----------------- |
| `id`         | Integer  | PK, autoincrement |
| `date`       | Date     | NOT NULL, UNIQUE  |
| `name`       | String   | NOT NULL          |
| `created_at` | DateTime | default `utcnow`  |

The UNIQUE constraint on `date` doubles as an index. The service queries:
`SELECT date FROM holidays WHERE date BETWEEN :start AND :end`.

---

### `leave_deductions`

| Column             | Type     | Constraints                                 |
| ------------------ | -------- | ------------------------------------------- |
| `id`               | Integer  | PK, autoincrement                           |
| `leave_request_id` | Integer  | FK → `leave_requests.id`, NOT NULL, indexed |
| `year`             | Integer  | NOT NULL                                    |
| `days`             | Float    | NOT NULL, CHECK (`days > 0`)                |
| `created_at`       | DateTime | default `utcnow`                            |

UNIQUE constraint on `(leave_request_id, year)`. This is the database-level safety
net for exactly-once approval: even if two transactions somehow both believed they won
the status CAS, the second INSERT would fail at this constraint before any balance
corruption could land.

One row per calendar year per approved request. A year-spanning request produces exactly
two rows. Cancellation reads these rows, reverses each balance entry, then deletes (or
zeros) the rows — so a second cancel finds nothing to reverse and is a true no-op.

---

### Enums

**`LeaveType`:** `annual`, `sick`, `personal`, `maternity`, `paternity`, `unpaid`

**`LeaveStatus`:** `pending`, `approved`, `rejected`, `cancelled`

State transitions allowed:

- `pending → approved` (via review, CAS)
- `pending → rejected` (via review, CAS)
- `pending → cancelled` (via cancel, CAS)
- `approved → cancelled` (via cancel, CAS)
- All other transitions are rejected with `REQUEST_NOT_PENDING` or
  `ALREADY_CANCELLED` / `REJECTED_NOT_CANCELLABLE`.

---

## 3. Edge Cases Identified

**Concurrent approvals (two managers click simultaneously).** Two `POST /leave-requests/{id}/review`
calls arrive in separate threads with the same `leave_request_id`. Both issue
`UPDATE leave_requests SET status='approved' WHERE id=:id AND status='pending'`.
The database executes these serially (SQLite serializes writers; PostgreSQL takes a
row lock on the `UPDATE`). The first caller gets `rowcount=1` and proceeds to write
balance and deductions in the same transaction. The second caller gets `rowcount=0`,
rolls back, and raises `RequestNotPendingError` → `409 REQUEST_NOT_PENDING`. The
balance is decremented exactly once.

**Manager double-click idempotency.** Sequential calls from the same session. The
second call hits the same `WHERE id=:id AND status='pending'` guard. Because the
first call committed `status='approved'`, the WHERE clause no longer matches, `rowcount=0`,
and the second call returns `409 REQUEST_NOT_PENDING`. No extra deduction occurs.

**Balance changed between submission and approval.** At submission, the balance
is sufficient. Before approval, another request for the same employee is approved,
consuming the remaining days. The approval service re-reads `leave_balances.used_days`
inside the approval transaction and re-checks that the balance still covers the
deduction. If not, it raises `InsufficientBalanceError` and the request stays pending.
The employee and the manager each see a clear message without any balance corruption.

**Self-approval blocked.** The service compares `approver_id` with
`leave_request.employee_id` before touching the database. If equal, raises
`SelfApprovalError` → `403 SELF_APPROVAL`. The check also verifies that `approver_id`
equals the employee's recorded `manager_id`; anyone else returns `403 NOT_AUTHORIZED_APPROVER`.

**Own-leaves overlap detection (pending + approved only).** When creating a request,
the service queries for existing `leave_requests` belonging to the same employee where
`status IN ('pending', 'approved')` and the date ranges overlap
(`existing.start_date <= new.end_date AND existing.end_date >= new.start_date`).
Cancelled and rejected requests are excluded — they no longer occupy the calendar and
should not block new submissions.

**Half-day on weekend or holiday contributes zero.** `leave_math.py` checks each day
in the range individually. If `start_date` is a Saturday and `half_day_start=true`, that
day contributes 0.0, not 0.5. A request that reduces to zero working days is rejected
with `422 NO_WORKING_DAYS` rather than silently stored as a zero-day request.

**Year-spanning leave (cross-year deduction split).** When `start_date.year != end_date.year`,
`leave_math.partition_by_year` produces a dict `{year: days}` with one entry per calendar
year. Each entry is checked against the corresponding `LeaveBalance` at submission time and
again at approval time. Both balances must be sufficient; if either fails, the whole
request is rejected with `INSUFFICIENT_BALANCE` naming the year that failed. On approval,
one `leave_deductions` row is written per year, and one `leave_balances.used_days` update
is issued per year — all inside the same transaction.

**Cancellation restores from `leave_deductions`, not recomputed.** When a previously
approved request is cancelled, the service reads every `leave_deductions` row for that
request and reverses each one. The holiday list at cancel time is irrelevant: if a holiday
was removed from the calendar between approval and cancellation, the restoration amount is
still exactly what was deducted. Recomputing from dates would silently over- or
under-restore the balance.

**Second cancel is a no-op.** The cancel service issues
`UPDATE leave_requests SET status='cancelled' WHERE id=:id AND status IN ('pending','approved')`.
If the request is already `cancelled`, `rowcount=0`, and the service raises
`AlreadyCancelledError` → `409 ALREADY_CANCELLED`. No balance query is reached.

**Rejected request cannot be cancelled.** The same CAS `WHERE` clause excludes `rejected`
status. If a rejected request is passed to cancel, `rowcount=0`. The service distinguishes
"already cancelled" from "rejected" by reading the current status after the failed update,
and raises `RejectedRequestNotCancellableError` → `409 REJECTED_NOT_CANCELLABLE`.

**Missing balance row returns zero, not error.** `get_leave_balances` queries the
`leave_balances` table and then synthesizes entries for any `LeaveType` enum value that
has no row for the requested `(employee_id, year)`. The caller always receives a full
set of entries. This matters most when a manager reviews a new employee who has never
taken a certain leave type: the API returns `total=0, used=0, remaining=0` rather than
an empty array or a 404.

---

## 4. Tradeoffs and Decisions

### Atomic compare-and-swap on `status` vs. alternatives

The headline correctness requirement is that `LeaveBalance.used_days` is never wrong,
even under concurrent approvals. Four approaches were evaluated:

1. **Atomic CAS on the request status (chosen).** A single conditional UPDATE:

   ```sql
   UPDATE leave_requests
      SET status='approved', approved_by=:approver, approved_at=:now
    WHERE id=:id AND status='pending';
   ```

   The driver's `rowcount` is the decision signal. If `rowcount == 1`, this caller is
   the unique winner; the balance update and `leave_deductions` insert follow in the
   same transaction and commit together. If `rowcount == 0`, someone else already moved
   the request out of `pending`; this caller rolls back and returns `409`.

   The `leave_deductions` UNIQUE constraint on `(leave_request_id, year)` is a
   second line of defence: even if two transactions somehow both believe they won the
   CAS (which cannot happen under correct isolation, but guards against future logic
   bugs), the second INSERT fails at the constraint before any balance drift lands.

   Full rationale recorded in **[docs/adr/001-atomic-state-transition.md](docs/adr/001-atomic-state-transition.md)**.

2. **Pessimistic row locks (`SELECT ... FOR UPDATE`).** Considered and rejected.
   SQLite does not support `FOR UPDATE`. On PostgreSQL, CAS is equally correct and
   needs no locking ceremony. Adopting `SELECT FOR UPDATE` would lose portability for
   no correctness gain at this scale.

3. **Optimistic concurrency with a `version` column.** Considered. Requires the caller
   to round-trip a version number on every approval, complicating both the API contract
   and the test matrix. Contention on a single request is rare (a handful per company
   per day); the extra client-side ceremony is not worth the win. Explicitly rejected in
   the ADR.

4. **Event-sourcing / ledger-derived balance.** The right answer at large scale. An
   append-only log of approve/cancel events, with balance derived by summing the log,
   gives a perfect audit trail and lets balances be reconstructed at any point in
   history. For a single-process SQLite demo, the write-side table and the projection
   add complexity without solving the actual correctness problem better than CAS.
   Called out as the upgrade path (see "What I Would Do With More Time"); not built.

---

### Deduction stored, not recomputed at cancel time

When a request is approved, every per-year deduction amount is written to
`leave_deductions`. Cancellation reads and reverses those rows. The alternative —
recomputing the deduction from `start_date`/`end_date` at cancel time — would silently
produce the wrong restoration amount if the holiday list changed between approval and
cancellation. Storing the deduction makes the cancel path independent of the current
holiday calendar. The `leave_deductions` UNIQUE constraint also provides the exactly-once
safety net described above.

---

### `src/leave_math.py` as a pure module

Day-counting logic (weekday counting, holiday exclusion, half-day rules, year-boundary
partitioning) lives in a separate module that takes only primitive inputs (`date`
objects and a `set[date]` of holidays) and returns only a number or a dict. No
SQLAlchemy, no I/O, no side effects.

This isolation serves two purposes. First, it makes the logic independently unit-testable:
the test suite can cover every combination of weekends, holidays, half-days, and
year-spanning ranges without setting up a database. Second, it prevents the creation of
a function that "just does a quick holiday lookup" inside the arithmetic, which would
couple the math to the DB and make tests expensive.

---

### SQLite for the demo

SQLite is the default because it requires no installation and the `make run` command
works out of the box. The CAS pattern does not depend on `SELECT FOR UPDATE` — it works
identically on PostgreSQL and MySQL. The `DATABASE_URL` environment variable in
`src/database.py` is the only point of change for a production deployment.

The one constraint that SQLite imposes is that the concurrency test uses a temporary
file-backed database (not `:memory:`), because in-memory SQLite opens a separate
in-process database per connection, which would make two sessions invisible to each
other and give a false-positive result.

---

### Caller-trusted identity

`approver_id` is passed in the request body and `employee_id` for cancellation is a
query parameter. The service enforces the business rules (manager-of-employee,
owner-of-request) but cannot verify that the caller is who they claim to be. This is an
explicit demo limitation. A production deployment must front this API with a
session-based or JWT auth layer that injects the authenticated user identity rather than
accepting it from the caller.

---

## 5. What I Would Do With More Time

**Alembic migrations.** The current setup calls `Base.metadata.create_all` on startup,
which is acceptable for a demo but destructive in production (cannot evolve schema
without dropping the database). Adding Alembic would give incremental, reversible
migrations with a full history.

**Real authentication.** Currently `approver_id` and `employee_id` are caller-supplied
and trusted. A real deployment needs a session or JWT layer that reads the authenticated
user from the token and injects it into the service calls, removing the caller's ability
to impersonate anyone.

**Event-sourcing / full audit trail.** The upgrade path from the current CAS approach:
replace `leave_deductions` with a proper event log (`LeaveEvent` with `event_type`,
`actor_id`, `payload`, and `occurred_at`). Balance becomes a projection over the log.
This gives a complete history for HR investigations, enables replaying state at any
point in time, and makes multi-region conflict resolution tractable. The rough trigger
for this upgrade is when audit requirements force a full event history, or when the
system grows beyond a single write region.

**Team-coverage rules.** The spec explicitly defers "no more than N teammates out at
once." The data model already has `department` on `Employee`. Adding a coverage check
is a query (`COUNT of approved/pending requests in department on overlapping dates`)
that could be evaluated during `create_leave_request` or `approve_leave_request`.

**Idempotency-key header for retry safety at the API layer.** The current implementation
handles double-clicks and concurrent approvals, but a network retry after a timeout
returns `409 REQUEST_NOT_PENDING` even though the underlying effect succeeded. An
`Idempotency-Key` header stored server-side (keyed by `(user_id, idempotency_key)`) would
let the server replay the cached response on a retry without re-executing the operation.
This is orthogonal to the CAS mechanism — both are needed for full safety.

**PostgreSQL for production.** The `DATABASE_URL` env var is the only change needed.
The CAS pattern, the UNIQUE constraint, and the transaction boundaries all work unchanged.
PostgreSQL also enables adding `SELECT FOR UPDATE SKIP LOCKED` for any future queue-like
patterns, proper ENUM types, and `RETURNING` clauses that eliminate a round-trip.

**Soft-delete for requests.** Currently rejected and cancelled requests remain visible in
filtered lists. Soft-delete (`deleted_at` column) would allow HR to clean up history
without losing the audit record, and would make the overlap query more explicit
(`WHERE deleted_at IS NULL AND status IN ('pending', 'approved')`).

**`reason` field PII handling.** The `reason` field can contain sensitive disclosures
(medical conditions, family circumstances). Currently it is stored as-is, returned in
full on all read endpoints, and the spec explicitly notes it should not appear in logs.
With more time: restrict `reason` to the request owner and their manager in the response
serializer, add it to a field-level encryption or masking policy, and audit-log access.

---

## 6. Running the Project

```bash
# Install
pip install -r requirements.txt

# Run
uvicorn src.app:app --reload

# Test
python -m pytest tests/
```

Interactive API documentation is available at `http://localhost:8000/docs` once the
server is running.

To reset the demo database (required after any schema change):

```bash
rm -f kakitangan.db
```

The database is re-created and re-seeded on the next server startup via
`Base.metadata.create_all` and the `seed_demo_data` call in the `startup` event handler.
