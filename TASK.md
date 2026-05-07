# TASK.md — Leave Management System Implementation

Priority order: database → service → middleware → API → unit tests → OpenAPI

---

## Phase 1 — Database Layer (`src/models.py`)

| # | Task | Status |
|---|------|--------|
| 1.1 | Add `LeaveDuration` enum — `full`, `first_half`, `second_half` | Complete |
| 1.2 | Rename `approved_by` → `reviewed_by`, `approved_at` → `reviewed_at` on `LeaveRequest` | Complete |
| 1.3 | Add `rejection_reason VARCHAR NULL` to `LeaveRequest` | Complete |
| 1.4 | Add `duration VARCHAR NOT NULL DEFAULT 'full'` to `LeaveRequest` | Complete |
| 1.5 | Change `SqlEnum` → `String` on `leave_type`, `status` columns (per DESIGN.md 4.15 — portable, human-readable raw SQL) | Complete |
| 1.6 | Add composite index `ix_leave_requests_employee_status_dates` on `(employee_id, status, start_date, end_date)` | Complete |
| 1.7 | Add UNIQUE constraint `uq_balance` on `LeaveBalance(employee_id, leave_type, year)` | Complete |
| 1.8 | Create `PublicHoliday` model — `id` PK, `date` DATE UNIQUE NOT NULL, `name` VARCHAR NOT NULL, `created_at`, `updated_at` | Complete |
| 1.9 | Replace all `datetime.utcnow` → `datetime.now(datetime.UTC)` across all models | Complete |

---

## Phase 2 — Service Layer (`src/services.py`)

| # | Task | Status |
|---|------|--------|
| 2.1 | Create `count_working_days(start_date, end_date, duration, db)` utility — counts Mon–Fri days in range, excludes dates present in `public_holidays` table. Returns float (0.5 for half-day, N for full-day range). | Complete |
| 2.2 | Implement `create_leave_request` — validate: employee exists, `start_date >= today`, `start_date <= end_date`, cross-year reject (422 — split into two requests), half-day must be single-day, half-day must be weekday, overlap check (all types + durations, only `pending`/`approved`, `first_half` and `second_half` on same day collide → reject with message to cancel and create full-day), balance check (unpaid skips cap, all types require a balance row to exist), immediate `used_days` increment | Complete |
| 2.3 | Rename & implement `review_leave_request` (was `approve_leave_request`) — validate: request exists, status is `pending`, reviewer is the direct manager (`requester.manager_id == reviewer.id`) OR reviewer is top-level with `manager_id IS NULL` doing self-review, reviewer != requester (unless top-level). On approve: no balance change (already deducted). On reject: restore `used_days`. Use conditional `UPDATE ... WHERE status='pending'` + rowcount check for concurrency safety. `rejection_reason` optional. | Complete |
| 2.4 | Implement `cancel_leave_request` — validate: caller is owner, status is `pending` or `approved` (not `rejected`/`cancelled`), `start_date` is not in the past. Restore balance via conditional UPDATE. If balance row missing, still cancel but log warning (graceful degradation). | Complete |
| 2.5 | Implement `get_leave_request` — fetch single leave request by ID. Auth scope: caller must be the request owner or the owner's direct manager (via `manager_id`). 404 if not found or outside scope. | Complete |
| 2.6 | Implement `get_leave_requests` — scope to caller's own requests + direct reports' requests (where `manager_id == caller_id`). `employee_id` filter narrows to a specific direct report. Date filter uses interval overlap: `start_date <= to_date AND end_date >= from_date`. Offset-based pagination. | Complete |
| 2.7 | Implement `get_leave_balances` — filter by employee_id, year defaults to current year | Complete |
| 2.8 | Implement `list_employees` — returns direct reports only (`manager_id == caller_id`), paginated | Complete |
| 2.9 | Implement `get_employee` — fetch employee + balances, 404 if not found | Complete |
| 2.10 | Implement `list_holidays` — optional `year` filter, paginated | Complete |
| 2.11 | Implement `create_holiday` — validate no duplicate date | Complete |
| 2.12 | Implement `update_holiday` — validate holiday exists, no duplicate date (excluding self) | Complete |
| 2.13 | Implement `delete_holiday` — validate holiday exists | Complete |
| 2.14 | Update `seed_demo_data` — ensure Alice has `manager_id=NULL`, add unpaid leave balance rows (total_days=0) for all employees, add Malaysian public holidays for 2026 | Complete |

---

## Phase 3 — Middleware Layer

| # | Task | Status |
|---|------|--------|
| 3.1 | Create `get_current_employee` FastAPI dependency — parses `Authorization: Bearer {employee_id}` header, returns `employee_id` as int. Missing/malformed → 401. | Pending |
| 3.2 | Create `require_manager` dependency — loads employee from `get_current_employee`, returns 403 if `employee.manager_id IS NOT NULL` | Pending |

---

## Phase 4 — API Layer (`src/app.py`)

| # | Task | Status |
|---|------|--------|
| 4.1 | Add `/api/v1` prefix via `APIRouter` mounted on the app | Pending |
| 4.2 | Convert all Pydantic schemas to v2 style (`model_config = ConfigDict(from_attributes=True)`), add `Field(description=...)` on all fields for OpenAPI | Pending |
| 4.3 | `LeaveRequestCreate` — remove `employee_id`, add `duration: LeaveDuration` field | Pending |
| 4.4 | `LeaveRequestOut` — `approved_by`→`reviewed_by`, `approved_at`→`reviewed_at`, add `rejection_reason`, `duration` | Pending |
| 4.5 | Rename `LeaveRequestApprove` → `LeaveRequestReview` — add `decision: Literal["approved","rejected"]`, `rejection_reason: Optional[str]` fields | Pending |
| 4.6 | Add schemas: `HolidayOut`, `HolidayCreate`, `HolidayUpdate`, `PaginatedEmployees`, `PaginatedHolidays`, `EmployeeWithBalancesOut` | Pending |
| 4.7 | `GET /api/v1/employees` — wire to `list_employees` service via auth dep; return paginated | Pending |
| 4.8 | `GET /api/v1/employees/{id}` — wire to `get_employee` service via auth dep | Pending |
| 4.9 | `POST /api/v1/leave-requests` — employee_id from auth dep, add duration; wire to `create_leave_request` | Pending |
| 4.10 | `GET /api/v1/leave-requests` — wire to `get_leave_requests` via auth dep | Pending |
| 4.11 | `GET /api/v1/leave-requests/{id}` — wire to `get_leave_request` service via auth dep | Pending |
| 4.12 | `POST /api/v1/leave-requests/{id}/review` — reviewer_id from auth dep; wire to `review_leave_request` | Pending |
| 4.13 | `POST /api/v1/leave-requests/{id}/cancel` — employee_id from auth dep, remove query param; wire to `cancel_leave_request` | Pending |
| 4.14 | `GET /api/v1/leave-balances/{id}` — wire to `get_leave_balances` via auth dep | Pending |
| 4.15 | `GET /api/v1/holidays` — any authenticated employee | Pending |
| 4.16 | `POST /api/v1/holidays` — gated behind `require_manager` | Pending |
| 4.17 | `PUT /api/v1/holidays/{id}` — gated behind `require_manager` | Pending |
| 4.18 | `DELETE /api/v1/holidays/{id}` — gated behind `require_manager` | Pending |
| 4.19 | Add OpenAPI tags — `employees`, `leave-requests`, `leave-balances`, `holidays` | Pending |
| 4.20 | Replace deprecated `on_event("startup")` with lifespan context manager | Pending |

---

## Phase 5 — Unit Tests (`tests/test_services.py`)

All tests use in-memory SQLite. Each test method seeds fresh data via `seed_demo_data`.

| # | Task | Status |
|---|------|--------|
| 5.1 | `test_create_leave_request_success` — Bob requests 3-day annual leave → 201, balance deducted by working days | Pending |
| 5.2 | `test_create_leave_request_half_day` — Bob requests first_half single day → deducts 0.5 | Pending |
| 5.3 | `test_create_leave_request_insufficient_balance` — Bob requests 15 days annual (only 14 available) → `InsufficientBalanceError` | Pending |
| 5.4 | `test_create_leave_request_overlapping` — Bob has pending 05-10→05-12; second request 05-11→05-13 → `OverlappingLeaveError` | Pending |
| 5.5 | `test_create_leave_request_half_day_same_day_collision` — Bob has pending first_half 05-10; second request second_half 05-10 → `OverlappingLeaveError` | Pending |
| 5.6 | `test_create_leave_request_start_after_end` — start_date > end_date → `LeaveError` | Pending |
| 5.7 | `test_create_leave_request_backdating` — start_date < today → `LeaveError` | Pending |
| 5.8 | `test_create_leave_request_cross_year` — 2026-12-28 → 2027-01-04 → rejected (cross-year) | Pending |
| 5.9 | `test_create_leave_request_half_day_multi_day` — first_half spanning 2 days → rejected | Pending |
| 5.10 | `test_create_leave_request_half_day_on_weekend` — first_half on Saturday → rejected | Pending |
| 5.11 | `test_create_leave_request_unpaid` — Unpaid leave with total_days=0 → succeeds, remaining_days goes negative | Pending |
| 5.12 | `test_create_leave_request_no_balance_row` — Leave type with no balance row → rejected | Pending |
| 5.13 | `test_create_leave_request_employee_not_found` — Non-existent employee_id → `LeaveError` | Pending |
| 5.14 | `test_review_approve` — Alice approves Bob's pending request → status approved, balance unchanged | Pending |
| 5.15 | `test_review_reject_restores_balance` — Alice rejects Bob's pending request → status rejected, `used_days` restored | Pending |
| 5.16 | `test_review_self_review_blocked` — Bob tries to review own request → error (Bob has manager_id=alice) | Pending |
| 5.17 | `test_review_self_review_top_level_allowed` — Alice (manager_id=NULL) reviews own request → succeeds | Pending |
| 5.18 | `test_review_not_direct_manager` — Carol tries to review Bob's request → error (Carol is not Bob's manager) | Pending |
| 5.19 | `test_review_already_reviewed` — Approve, then approve again → error (status not pending) | Pending |
| 5.20 | `test_cancel_pending` — Bob cancels own pending request → status cancelled, balance restored | Pending |
| 5.21 | `test_cancel_approved` — Alice approves, then Bob cancels → status cancelled, balance restored | Pending |
| 5.22 | `test_cancel_not_owner` — Carol tries to cancel Bob's request → error | Pending |
| 5.23 | `test_cancel_rejected` — Rejected request → cannot cancel | Pending |
| 5.24 | `test_cancel_already_cancelled` — Already cancelled → cannot cancel again | Pending |
| 5.25 | `test_cancel_past_start_date` — Leave start_date is yesterday → cannot cancel | Pending |
| 5.26 | `test_get_leave_requests_scoped` — Alice sees Bob + Carol + self; Bob sees only self | Pending |
| 5.27 | `test_get_leave_requests_date_filter_overlap` — Request 04-20→04-30; filter from_date=04-23, to_date=04-25 → should match (interval overlap) | Pending |
| 5.28 | `test_get_leave_requests_date_filter_no_match` — Request 04-20→04-30; filter from_date=05-01, to_date=05-10 → no match | Pending |
| 5.29 | `test_get_leave_requests_pagination` — Page 1 size 1, page 2 size 1, page beyond data returns empty | Pending |
| 5.30 | `test_list_employees_direct_reports` — Alice (manager) sees Bob + Carol; Bob (non-manager) sees empty | Pending |
| 5.31 | `test_get_leave_balances_default_year` — Year omitted → current year balances | Pending |
| 5.32 | `test_get_leave_balances_empty` — New employee with no balance rows → empty list | Pending |
| 5.33 | `test_holiday_create` — Alice (manager) creates holiday → 201 | Pending |
| 5.34 | `test_holiday_create_duplicate_date` — Same date twice → error | Pending |
| 5.35 | `test_holiday_crud_non_manager` — Bob (non-manager) tries to create → error | Pending |
| 5.36 | `test_holiday_list_by_year` — Filter by year returns correct subset | Pending |
| 5.37 | `test_holiday_update_delete` — Update name, delete → 204 | Pending |
| 5.38 | `test_count_working_days` — Excludes weekends, excludes holidays, all-weekend range returns 0 | Pending |
| 5.39 | `test_holiday_on_weekend` — Holiday on Saturday is allowed (no rejection), working-day counter already skips weekends | Pending |

---

## Phase 6 — Integration Tests (`tests/test_api.py`)

| # | Task | Status |
|---|------|--------|
| 6.1 | `test_auth_missing_header` — No Authorization header → 401 on any endpoint | Pending |
| 6.2 | `test_auth_malformed_header` — `Authorization: Invalid thing` → 401 | Pending |
| 6.3 | `test_list_employees` — Alice (id=1) sees Bob + Carol with pagination | Pending |
| 6.4 | `test_get_employee` — GET employee/2 → 200 with employee + balances | Pending |
| 6.5 | `test_get_employee_404` — Non-existent ID → 404 | Pending |
| 6.6 | `test_create_leave_request` — Full create flow → 201, verify response shape | Pending |
| 6.7 | `test_create_leave_request_validation_error` — Invalid dates → 422 with error detail | Pending |
| 6.8 | `test_review_leave_request` — Manager approves → 200, status=approved | Pending |
| 6.9 | `test_cancel_leave_request` — Owner cancels → 200, status=cancelled | Pending |
| 6.10 | `test_cancel_not_owner` — Wrong employee → 403 | Pending |
| 6.11 | `test_leave_balances` — GET balances → 200 with array | Pending |
| 6.12 | `test_holiday_crud` — Manager: create → 201, list → 200, update → 200, delete → 204 | Pending |
| 6.13 | `test_holiday_unauthorized` — Non-manager POST → 403 | Pending |
| 6.14 | `test_pagination_edge_cases` — page=0 → 422, page_size=200 → capped/422, page beyond data → empty | Pending |

---

## Phase 7 — OpenAPI Verification

| # | Task | Status |
|---|------|--------|
| 7.1 | Verify all Pydantic schemas render with `Field` descriptions at `/docs` | Pending |
| 7.2 | Verify all routes appear under correct tags with `response_model` and status codes | Pending |
| 7.3 | Manually confirm `/docs` interactive docs are usable (try a request) | Pending |
| 7.4 | Run `make test` — all tests pass | Pending |
| 7.5 | Run `make run` — server boots without errors | Pending |

---

## Notes

- **Half-day same-day collision:** A `first_half` and `second_half` on the same date are treated as overlapping — reject with message to cancel one and create a full-day request instead.
- **Unpaid leave:** `total_days=0` balance row must exist. `used_days` increments normally; `remaining_days` goes negative. Only the balance cap check is skipped.
- **Seed data:** Include Malaysian public holidays for 2026 (New Year's, Chinese New Year, Hari Raya Puasa, Labour Day, Wesak Day, Agong's Birthday, Hari Raya Haji, Merdeka Day, Malaysia Day, Deepavali, Christmas, Awal Muharram).
- **Testing auth:** Integration tests set the `Authorization: Bearer {id}` header via `TestClient`. Unit tests call service functions directly with explicit `employee_id`/`reviewer_id` params.
