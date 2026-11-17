# Writing Integration Tests (Phase 4, level: integration)

Contents: scope · what is real vs doubled · derivation from the spec · setup and isolation · assertions · failure paths · infrastructure that is unavailable · flakiness

## Scope

An integration test checks that the feature works with its **real collaborators**: the actual database (or a test instance), filesystem (temp directory), message queue, other internal modules, and the real wiring between them. It catches what unit tests cannot: wrong queries, serialization mismatches, transaction behavior, misconfigured dependencies.

Directory: `tests/integration/` (or the repo's equivalent).

## Real versus doubled

| Real | Doubled |
|---|---|
| Your own database schema and queries (test DB or container) | Third-party HTTP APIs, payment providers, email/SMS senders |
| Your own modules and their wiring | The system clock and randomness when behavior depends on them |
| Filesystem in a temp directory | Anything costly, non-deterministic, or outside your system |
| Local queues/caches run for tests | |

A double at an external boundary should behave like the real thing, including failures: timeouts, error responses, malformed payloads. Prefer a recorded or contract-shaped fake over a bare mock returning a hard-coded success.

## Derive tests from the spec

For each requirement at level `integration`:
- Convert examples into scenarios: set up state (Given), perform the operation through the same entry point callers use (When), assert observable outcomes (Then).
- Convert error and edge cases into failure scenarios (see below).
- Convert invariants into property-based tests where useful: for example "no sequence of operations leaves a negative balance". Keep them small and bounded; see `property-tests.md`.
- Each test names its requirement ID.

Do not test internals of the unit again; unit tests own that. Integration tests own **the seams**.

## Setup and isolation

- Each test creates and cleans up its own data. No dependence on another test's leftovers or on execution order.
- Use a throwaway database/schema or transaction rollback per test, temp directories for files, unique names/IDs for shared resources.
- Keep fixtures explicit and minimal; builders over giant fixture files.
- Do not point tests at shared, long-lived, or production-like environments.

## Assertions

Assert on **observable outcomes**:
- Persisted state (query the real store after the operation)
- Returned responses (status, body shape, values from the spec)
- Emitted events or messages
- Absence of side effects when the spec says nothing should happen (e.g., invalid input creates no record)

Use exact values from the spec. Avoid asserting on generated IDs or timestamps except by shape or by injected values.

## Failure paths (do not skip)

For every external dependency the feature touches, cover what the spec says about:
- Dependency returns an error or times out
- Partial failure: the first step succeeds and the second fails. Assert the resulting state (rolled back, compensated, or left as the spec states)
- Invalid input arriving at the boundary
- Repeat or retried calls when the spec states idempotency

If the spec says nothing about a failure that clearly can occur, list it as an ambiguity in the Gate 3 summary.

## When required infrastructure is not available

Do not replace the real collaborator with a mock just to get green; that turns an integration test into a unit test. Instead:
1. Say what is missing (for example, no test database).
2. Propose the smallest fix (a container, an in-memory equivalent that is faithful for the behavior under test, a local service).
3. Ask the user before installing or starting services.
4. If tests cannot run yet, still write them, and report in the red-run results that they could not be executed and why.

## Flakiness rules

- No fixed sleeps; poll with a timeout or wait on a condition.
- Fixed seeds for any randomness; injected clocks.
- Explicit timeouts on every external call and on the test itself.
- Keep the number of integration tests modest. Push logic-level checks down to unit tests.
