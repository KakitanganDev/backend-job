# Design Document — Leave Management System

**Author:** Nabil
**Date:** 2026-05-07

This document explains the choices behind the implementation in `src/`.
Everything here is opinionated. Where a decision could reasonably go the
other way, I've tried to explain *why* I went the way I did and what I
would do differently in a real production setting.

---

## 1. API design

All mutating endpoints expect an `X-Employee-Id` header that identifies
the acting user. See §4 on auth — this is a deliberate stub.

| Method | Path | Body / params | Success | Notable failures |
|---|---|---|---|---|
| `GET`  | `/health` | — | 200 | — |
| `GET`  | `/employees` | — | 200 list | — |
| `GET`  | `/employees/{id}` | — | 200 with balances | 404 |
| `GET`  | `/leave-balances/{employee_id}` | `?year=` | 200 | — |
| `POST` | `/leave-requests` | `LeaveRequestCreate` | 201 | 401 missing header · 403 acting for another · 422 validation/balance/overlap · 404 employee |
| `GET`  | `/leave-requests` | filters + `page`,`page_size` | 200 paginated | 422 |
| `GET`  | `/leave-requests/{id}` | — | 200 | 404 |
| `POST` | `/leave-requests/{id}/review` | `decision: approved\|rejected` | 200 | 403 self/non-manager · 409 already reviewed · 422 insufficient balance · 404 |
| `POST` | `/leave-requests/{id}/cancel` | — | 200 | 403 non-owner · 409 not cancellable · 404 |

### Error mapping

Domain errors (`LeaveError` subclasses) are mapped to HTTP statuses by
`_map_leave_error` in `src/app.py`:

- `404` for `EmployeeNotFoundError`, `LeaveRequestNotFoundError`
- `403` for `SelfApprovalError`, `NotAuthorizedError`
- `409` for `CannotModifyApprovedLeaveError` (state-conflict — request is
  no longer in the right state to mutate)
- `422` for everything else — validation, balance, overlap, missing-balance

I picked 409 over 422 for state-conflict deliberately: 409 communicates
"the resource exists but is in a state that doesn't allow your action,"
which is what a re-approval attempt actually is. 422 is reserved for
"your inputs are well-formed JSON but semantically invalid."

### Pagination

Offset + page_size, returning `(items, total)`. I considered cursor
pagination but skipped it for V1 — see §4.

---

## 2. Data model

Three tables, defined in `src/models.py`. SQLAlchemy 2.0 declarative,
typed enums, with constraints enforced both at the SQL and ORM levels.

### `employees`
- `id` PK
- `email` unique, indexed
- `manager_id` FK → `employees.id`, self-referential and nullable (the
  org has at least one root manager)
- timestamps

### `leave_requests`
- `id` PK
- `employee_id` FK, indexed
- `leave_type` enum
- `start_date`, `end_date` — `CHECK (start_date <= end_date)`
- `start_half_day`, `end_half_day` bool flags
- `working_days` float — **snapshot** of the day count computed at
  submission time. Stored, not recomputed on demand. See §4.
- `status` enum (PENDING/APPROVED/REJECTED/CANCELLED), indexed
- `approved_by` FK → `employees.id`
- composite index `(employee_id, start_date, end_date)` for the overlap
  query and "list leaves in window" filters
- `CHECK (working_days >= 0)`

### `leave_balances`
- `id` PK
- `(employee_id, leave_type, year)` UNIQUE — prevents double-allocation
- `total_days`, `used_days` floats (Floats because half-days exist)
- `CHECK (total_days >= 0 AND used_days >= 0)`
- `remaining_days` is computed as a Python property; not denormalised

### Why a `working_days` snapshot column

The day count depends on (a) the date range, (b) the half-day flags,
and (c) the holiday calendar for that period. (c) is mutable — a
country might gazette a new public holiday retroactively, or a future
deploy might fix a bug in the calendar library. If we recomputed
`working_days` from scratch every time we needed it (e.g., to restore
the balance on cancellation), a calendar change between approval and
cancellation would cause the restored amount to differ from the
deducted amount, silently corrupting balances over time. Storing the
snapshot at submission and using it for both deduction and restoration
keeps balance accounting closed and auditable.

### Why no `version` column for optimistic locking

I evaluated optimistic locking and chose pessimistic (see §4). With
pessimistic locking + `SELECT FOR UPDATE`, a version column adds noise
without buying anything. Adding it later if we move to optimistic
locking is a one-migration change.

---

## 3. Edge cases identified

In rough order of how interesting they are:

1. **Two managers race to approve the same request.** Solved by
   pessimistic row lock on the `LeaveRequest` (`SELECT ... FOR UPDATE`)
   followed by status check. Test: `test_two_managers_race_only_one_wins`.
2. **Two requests draining the same balance concurrently.** Solved by
   `SELECT FOR UPDATE` on the `LeaveBalance` row inside the approval
   txn. Test: `test_concurrent_approvals_respect_balance_ceiling`.
3. **Cancel an approved leave restores the balance** under the same
   lock discipline as approval. Test: `test_cancel_approved_restores_balance`.
4. **Self-approval** (employee approves own leave) — explicit check
   against `lr.employee_id == approver_id`, raises `SelfApprovalError`.
5. **Non-manager approval** — only the requester's direct manager can
   approve. Skip-level approval (e.g., for absent managers) is out of
   scope; see §5.
6. **Re-approval / re-cancellation of a non-PENDING request** —
   raises `CannotModifyApprovedLeaveError`. Maps to 409.
7. **Back-dated requests** — `start_date < today` rejected.
8. **Inverted date range** (`start > end`) — rejected at service entry
   and also at the SQL `CHECK` constraint as defence in depth.
9. **Weekends and Malaysian public holidays** — excluded from the day
   count via `holidays.MY()`. A request whose entire span is non-working
   raises `NoWorkingDaysError`.
10. **Half-day leaves** — two booleans (`start_half_day`, `end_half_day`).
    For a single-day leave, both flags refer to the same day; we apply
    at most one 0.5 deduction. Half-day flags on a non-working boundary
    (e.g. end_half_day on a Saturday) are ignored — there's nothing to
    halve.
11. **Year-spanning leaves** — attributed entirely to the *start* year.
    See §4.
12. **Overlap detection** — uses the standard `A.start <= B.end AND
    B.start <= A.end` predicate, restricted to ACTIVE statuses
    (PENDING and APPROVED). CANCELLED and REJECTED leaves don't block
    new requests for the same dates.
13. **No balance allocated** for a balance-tracked leave type → distinct
    `BalanceNotAllocatedError`, not "insufficient balance" (different UX
    intent). UNPAID skips the balance check entirely.
14. **UNPAID leave with no balance row** — handled. UNPAID has no
    balance, so submission and approval skip the balance lookup.
15. **Pre-approval balance shrinkage** — if the manager shrinks the
    employee's balance between submission and approval, the approval
    re-checks `remaining_days >= working_days` and refuses if not. The
    submission-time check is for fast UX feedback; the approval-time
    check is the authoritative one.
16. **Idempotent submissions** — *not* implemented. A retried POST
    creates a duplicate request. See §5 for the standard remedy
    (`Idempotency-Key` header + unique partial index).

---

## 4. Tradeoffs and decisions

### 4.1 Concurrency: pessimistic `SELECT FOR UPDATE` (chose this)

**Alternatives considered:** optimistic versioning; status-guarded
compare-and-swap UPDATE.

**Why I chose pessimistic:**
- It's the easiest model to reason about, debug, and explain in a
  pull-request review. Two writers; second waits.
- The contention domain is naturally narrow — a single employee's
  balance row, or a single leave request — so we're not at risk of
  trapping wide swathes of writers behind one another.
- It composes cleanly with the multi-step transaction shape we have:
  read leave request, validate, read balance, mutate both, commit.
  Optimistic versioning would require either two CAS updates (with the
  retry loop becoming the dominant complexity), or moving the
  validation logic into a single conditional UPDATE per row, which
  fragments business rules across SQL and Python.

**Lock ordering** is fixed in `services.py`: LeaveRequest first, then
LeaveBalance. Because every mutating path follows this order, two
concurrent calls cannot deadlock.

**SQLite caveat.** SQLite has no row-level locking; `FOR UPDATE` is a
parser-accepted no-op. To preserve the same "second writer waits"
semantics under SQLite (so the SQLite-based tests are a faithful
proxy), the writer paths call `escalate_to_immediate(db)` at the start
of the transaction, which issues `BEGIN IMMEDIATE` to grab the
database-level write lock. WAL mode is enabled so this doesn't block
readers. Read-only paths don't escalate, so they retain default
deferred behaviour.

The concurrency test (`tests/test_concurrency.py`) opens two real
connections from two threads through a barrier and asserts:
1. exactly one approval succeeds when both target the same leave;
2. exactly one approval succeeds when two leaves race for a balance
   that can only fit one.

### 4.2 Calendar policy

**Chose:** working days (Mon–Fri) excluding **Malaysian federal**
public holidays via the `holidays` Python library, with half-day flags
on the boundaries.

**Considered and deferred:**
- **State-level MY holidays.** `holidays.MY()` only returns federal
  holidays. Sultan's birthdays differ across states, and a real HR
  product would need the employee's state of work to compute correctly.
  This is real complexity that needs an `Employee.state` field and a
  proper calendar service — out of scope here (§5).
- **Pluggable per-tenant calendars.** A SaaS HR product needs each
  customer to override the calendar (a Singapore tenant should not see
  MY holidays). The right shape is a `CalendarService` interface
  injected into `services.py`. I kept it inline behind a tight module
  boundary (`src/calendar.py`) so the swap is mechanical when needed.

**Half-day model.** Two booleans (`start_half_day`, `end_half_day`).
Considered a single `is_half_day` field but it doesn't extend to leaves
spanning multiple days where only one boundary is half. Considered
a `time_off_type` enum (FULL/AM/PM) but that's coupled to a Western
9-to-5 assumption I didn't want to bake in.

### 4.3 Year-spanning leaves: attribute-to-start-year (V1)

**Chose:** the entire leave deducts from the start year's balance.

**Alternative:** split by calendar year, deduct proportionally from
each year's balance.

**Why V1:**
- Year-spanning leaves are uncommon in practice. Most HR systems treat
  them as a single accounting unit booked in the year they start.
- The split version requires locking *two* balance rows (one per year)
  in deterministic order to avoid deadlock (`ORDER BY year ASC` on the
  lock acquisition). It's not hard to implement, but it doubles the
  lock surface and the test surface for a feature that's used rarely.
- The migration path to V2 is straightforward: keep the same lock
  pattern, deduct from each year's balance row in turn under the same
  transaction.

There's an explicit test (`test_year_spanning_attributes_to_start_year`)
that pins this behaviour so a future change to V2 will surface in CI.

### 4.4 Database choice: SQLite default, Postgres via compose

**Chose:** SQLite default for zero-setup local dev, Postgres in
docker-compose for production-shape tests.

**Why both:** reviewers should be able to run `make test` without
installing anything beyond Python. But the actual production shape
needs Postgres for proper row-level locking, and CI runs against
Postgres for the concurrency tests. Alembic migrations are
dialect-aware (with `render_as_batch` for SQLite).

### 4.5 Auth: `X-Employee-Id` header stub

**Chose:** mutating endpoints read an `X-Employee-Id` header and trust
its value. There is no signature, no token verification.

**Why stub:** the leave-management problem is what's being graded.
A real auth implementation (JWT issuance, refresh, RBAC, session
revocation) is its own project. Stubbing it via a header keeps the
auth boundary explicit and reviewable: every mutating endpoint takes
`actor_id: int = Depends(current_user_id)`, and the dependency is the
single point where production would swap to JWT verification.

**Why not query param:** less RESTful and a query param accidentally
leaks user identity into access logs.

### 4.6 Sync SQLAlchemy, not async

The leave operations are short, transactional, and CPU-trivial; the
session.py-style ergonomics of sync SQLAlchemy are a better fit than
the AsyncSession's awaiting overhead at this scale. If the workload
shifted to long-running fan-out (e.g., notifying many managers per
submission), async would be the right call.

### 4.7 Pagination: offset, not cursor

Cursor pagination is technically nicer (stable under inserts) but
needs a deterministic ordering and a serialisable cursor. For a leave
list filtered to one employee with realistic volumes (low hundreds of
items per employee per year), offset is fine and easier to reason
about. Cursor would be the right move at the org-wide listing scale
(thousands of items per page over a managerial team's reports).

---

## 5. What I would do with more time

In rough order of how much value I think they'd add:

1. **Real auth.** JWT issuance, RBAC ("manager", "hr_admin", "employee"),
   token revocation. The `current_user_id` dependency is already the
   single swap point.
2. **Idempotent submission.** Add an `idempotency_key` nullable column
   on `LeaveRequest` with a unique partial index. The route reads an
   `Idempotency-Key` header and the service short-circuits if the key
   already exists for that employee.
3. **Multi-tenancy.** A `tenant_id` on every table, Postgres row-level
   security policies tied to the JWT claim. This is what turns the
   single-org demo into a real SaaS.
4. **Accrual policy.** Right now `LeaveBalance.total_days` is set
   manually. A policy engine should accrue (e.g., 1.17 days per month
   pro-rated for join date), handle carryover and lapse rules, and
   re-balance on Jan 1.
5. **Skip-level approval.** If the direct manager has been on leave
   for N days, the request should fall back to their manager. Today
   it just sits in PENDING.
6. **State-aware MY holidays + per-tenant calendar override.** Move
   `src/calendar.py` behind a `CalendarService` interface; resolve the
   employee's state and tenant from context.
7. **Audit log.** Append-only `leave_request_events` table written on
   every status transition, with the actor and timestamp. Today the
   request row gets overwritten on each transition.
8. **Year-spanning split (§4.3 V2).**
9. **Background reconciliation job** that recomputes used-days from
   the audit log nightly and alerts on drift. Cheap insurance.
10. **Notification side effects.** Submission emails the manager;
    approval/rejection emails the requester. Behind a transactional
    outbox so we don't double-send on retries.
11. **OpenAPI client generation** for the frontend — FastAPI gives us
    the schema for free; just need a CI step.
12. **OTLP exporter** swap-in for OpenTelemetry. The console exporter
    is a demo placeholder.

---

## 6. Running the project

```bash
# Local with SQLite (zero setup)
make install
make test          # 45 tests, all green
make run           # http://localhost:8000  Swagger UI at /docs

# Postgres (matches production shape)
cp .env.example .env
# uncomment the DATABASE_URL line for postgres
make db-up
make db-migrate
make run

# Lint
make lint
```

### Try it (with the seeded demo data)

```bash
# Bob (id=2) submits a 1-day leave
curl -X POST http://localhost:8000/leave-requests \
  -H 'Content-Type: application/json' \
  -H 'X-Employee-Id: 2' \
  -d '{"employee_id":2,"leave_type":"annual",
       "start_date":"2026-06-15","end_date":"2026-06-15"}'

# Alice (id=1, Bob's manager) approves
curl -X POST http://localhost:8000/leave-requests/1/review \
  -H 'Content-Type: application/json' \
  -H 'X-Employee-Id: 1' \
  -d '{"decision":"approved"}'
```

Every response includes an `X-Request-ID` header that's threaded through
all log lines emitted while handling the request — handy for grepping
out a single request's flow when debugging.
