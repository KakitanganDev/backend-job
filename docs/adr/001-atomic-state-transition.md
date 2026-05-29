# ADR-001: Atomic state-transition (compare-and-swap) for leave-request approval

**Status:** Accepted
**Date:** 2026-05-29
**Context spec:** [docs/specs/TBD-leave-management-api/spec.md](../specs/TBD-leave-management-api/spec.md)

## Context

The leave-management system's headline correctness requirement is that a
`LeaveBalance.used_days` value is **never wrong** — not after concurrent
approvals on the same request, not after manager double-clicks, not after a
network retry replays an approval call. The discovery brief identified
"balance trust under concurrent operations" as the highest-depth opportunity
for the take-home, and committed to delivering an explicit concurrency test as
the falsifying experiment.

Four candidate solutions were on the table:

1. **Atomic state-transition on the request** — a single conditional `UPDATE`
   on `leave_requests.status` driven by SQL's affected-row count, wrapped with
   the balance update in one transaction.
2. **Event-sourced / ledger-derived balance** — append-only log of
   approve/cancel events; balance derived by summing the log.
3. **Optimistic concurrency with a `version` column** — caller round-trips a
   version number; updates fail if the version moved.
4. **Idempotent commands with request-id keys** — server-side dedupe keyed by
   a client-provided idempotency key.

## Decision

**Use option 1: atomic state-transition (compare-and-swap on `status`).**

Concretely:

```sql
UPDATE leave_requests
   SET status='approved', approved_by=:approver, approved_at=:now
 WHERE id=:id AND status='pending';
```

The driver returns `rowcount`. If `rowcount == 1`, this caller is the unique
winner — proceed to update `leave_balances.used_days` and insert
`leave_deductions` rows in the same transaction, then `COMMIT`. If
`rowcount == 0`, another caller (or a retry of this one) already moved the
request out of `pending` — `ROLLBACK` and raise `RequestNotPendingError`.

A UNIQUE constraint on `leave_deductions(leave_request_id, year)` is the
database-level safety net: even in the impossible event that two transactions
both believed they won, the second INSERT would fail at the constraint level
before drift could occur.

## Why not the alternatives

- **Ledger / event-sourcing (option 2)** is the right answer at scale. It
  gives a perfect audit trail and lets the balance be reconstructed at any
  point in history. For a single-process SQLite demo, the cost (a write-side
  table + a projection, plus careful thought about read-after-write
  consistency for the API) exceeds the value. Acknowledged in `DESIGN.md` as
  the upgrade path; not built.
- **Optimistic concurrency with a `version` column (option 3)** moves the
  problem to the API — clients must round-trip the version on every approval.
  At this scale, contention on the same request is rare (discovery brief: at
  most a handful per company per day). Retries dominate locks. The extra
  client-side ceremony is not worth the win.
- **Idempotency keys (option 4)** is orthogonal, not an alternative. It solves
  retry/double-click safety at the API edge, and is complementary to whatever
  concurrency mechanism the service uses underneath. Worth a paragraph in
  `DESIGN.md`; not the answer to the "two managers approve at the same
  instant" problem.

## Consequences

**Positive:**

- The "two managers approve simultaneously" scenario resolves to exactly one
  winner with no read-modify-write race. The test asserts this directly.
- Portable: the same pattern works unchanged on PostgreSQL, MySQL, SQLite. No
  `SELECT FOR UPDATE` required.
- Reads do not block writes; writes are serialized only on the row(s) being
  updated.
- The `leave_deductions` UNIQUE constraint provides a second line of defense
  against any future logic bug that might bypass the status CAS.

**Negative / accepted limitations:**

- Does not solve the "client sent the same approval twice with retries"
  problem at the network layer. The second retry sees `409 REQUEST_NOT_PENDING`
  and reports it as an error to the client, even though the underlying effect
  was successful on the first try. Mitigation: add an idempotency-key header
  in a future release — orthogonal to this decision.
- The pattern only works because the approval transaction is short and
  touches few rows. A multi-step workflow with side effects outside the
  database (notifications, external systems) would need a saga / outbox
  pattern on top — explicitly out of scope per the business spec ("no
  notifications").
- SQLite's writer-serialization helps us in tests; on PostgreSQL the same
  guarantee holds but via row-level locks taken by the `UPDATE` itself. Both
  are correct; the implementation does not need to know the difference.

## Forward pointers

- The concurrency test (`tests/test_services.py::test_concurrent_approval_exactly_once`)
  is the falsifying experiment for this decision. If the test ever fails on a
  clean run, this ADR's premise is broken — investigate before patching the
  test.
- `DESIGN.md` discusses the upgrade path to event-sourcing, when it becomes
  worth the cost (rough threshold: when audit requirements force a full
  event history, or when multi-region writes need conflict resolution).
