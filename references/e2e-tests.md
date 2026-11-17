# Writing End-to-End Tests (Phase 4, level: e2e)

Contents: scope · how many · rules · environment · UI specifics · when they cannot run yet · failure artifacts

## Scope

An e2e test runs the **real application through a real entry point** and asserts what a user or caller can observe: a CLI invocation and its output, an HTTP request to a running server and its response, a browser session driving the actual UI. It answers one question: does the feature work when everything is wired together?

Directory: `tests/e2e/` (or the repo's equivalent).

## How many

Few. Typically 1–3 per feature: the main happy path and the most important failure path. E2E tests are the slowest and the most prone to flakiness; behavior details belong in unit and integration tests. Write e2e only for requirements whose level includes `e2e` in the approved spec.

## Rules

- **No mocks of your own code.** Only truly external third parties may be faked (payment provider sandbox, email catcher), and only when a real one is unusable in tests.
- **Assert user-visible outcomes**, not internals: output text, response bodies, rendered content, exit codes, files produced.
- **Use the real entry point** the way callers do (same command, same URL, same UI flow).
- **Independent of internal structure.** The test must survive refactoring that does not change behavior.
- **Own your data.** Create what the test needs through public interfaces or a documented seed; clean up afterwards; unique identifiers per run.
- **Wait on conditions, not time.** No fixed sleeps; use explicit waits with timeouts.
- **Each test is runnable alone** and in any order.
- Name each test with its requirement ID, like the other levels.
- Expected values come from the spec examples, not from observing the running app.

## Environment

- Start the app the same way the project documents (script, compose file, task runner). Do not invent a different launch path.
- If starting the app needs services (database, queue), use the project's test configuration. If none exists, propose one and ask before creating infrastructure.
- Keep configuration for tests separate from production configuration.

## UI specifics

- Select elements by stable, user-meaningful handles (roles, labels, test IDs the project already uses), not by brittle CSS paths or text that changes often.
- Assert on what the user sees and can do, not on DOM structure.
- Prefer the project's existing browser automation tool (for example Playwright or Cypress); do not add another without asking.

## When they cannot run yet

Before implementation exists, e2e tests will fail or be unable to start. That is expected. In the Phase 5 summary, state whether each e2e test ran, failed for the right reason (the feature is absent), or could not start (and why). A test that cannot even start because of a harness problem should be fixed, not left broken.

## Failure artifacts

Configure the test to capture useful evidence on failure (logs, response bodies, screenshots) in the project's usual output location. This makes later debugging by the implementer possible without editing the test.
