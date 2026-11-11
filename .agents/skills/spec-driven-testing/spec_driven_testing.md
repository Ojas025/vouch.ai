---
name: spec-driven-testing
description: Writes and validates tests for ONE feature, function, or component from an existing specification. Does not create or edit the specification or implement the feature.
license: MIT
metadata:
  version: "1.0"
---

# Spec-Driven Testing

Turn an existing spec into tests, one feature at a time. Covers spec review, coverage plan, tests, and a red run, plus a mutation check after the user's implementation is done. Never writes the specification or implementation.

## Ground rules

1. **The user owns what is correct.** Derive tests from the existing spec. Propose interpretations when useful, but do not invent requirements. Label suggestions `agent-proposed` until accepted.
2. **Tests derive from the spec, never from the implementation.** Do not read the feature's implementation. Expected values come from the spec or hand-computed constants, never from running the code under test. Read declarations or public interfaces only when necessary.
3. **Three gates are hard stops.** After presenting a gate, end your turn and wait for explicit approval. If the user waives a gate, comply and record the waiver in the existing spec's status line.
4. **Order is spec → tests → implementation.** If a test seems wrong, stop and show the evidence. The user decides whether the spec or test needs correction. Do not edit tests just to make them pass. If the spec needs changing, ask the user to update it, then regenerate affected tests from the revised spec.
5. **Three test directories only:** `unit`, `integration`, and `e2e`. Property-based tests live inside unit and integration files.
6. **No specification editing or creation.** Do not create a spec or change its requirements. You may update the existing status line only to track workflow progress. If the spec is missing, ambiguous, contradictory, or insufficient to derive an expected result, report the gap and stop rather than filling it in yourself.
7. **No implementation code**, except a minimal "not implemented" stub when needed to run the red run.

## Workflow

Read the reference for a phase when you reach it, not before.

| Phase | Do | Read | Stop at |
|---|---|---|---|
| 0. Orient | Detect the stack, locate the existing spec and test directories, and identify the target feature. Do not inspect its implementation. | `references/orient-and-intake.md` | None |
| 1. Spec review and coverage | Check the relevant spec requirements for ambiguity, contradictions, and testability. Map each requirement to candidate tests and identify gaps without changing the spec. | `references/tests-phase.md` | **Gate 1:** user approves the spec interpretation and coverage plan |
| 2. Tests | Write unit, integration, and e2e tests as appropriate, including property-based tests where useful. Keep tests blind to implementation. Self-review the test set. | `references/tests-phase.md`, then `unit-tests.md`, `integration-tests.md`, `e2e-tests.md`, `property-tests.md` as relevant | **Gate 2:** user approves the written tests before execution |
| 3. Red run | Run tests against a minimal not-implemented stub where needed. Classify failures, fix test-harness issues only, and self-review. | `references/red-run-and-handoff.md` | **Gate 3:** user approves the tests; then give the hand-off block |
| 4. Mutation | After implementation, run a scoped mutation check, triage survivors, and propose spec-derived tests for the user's review. | `references/mutation-testing.md` | Done criteria met |

If the spec is marked `draft`, flag that it has not been approved and ask the user to confirm that it is authoritative for test derivation before proceeding. Do not resolve spec gaps on the user's behalf.

## Status line and resuming

The existing spec header may carry `Status: draft | approved | tests-approved | implemented | mutation-checked`. Do not create a missing spec or rewrite its contents. Update only this existing status line when needed.

On re-invocation, read the status and inspect the current tests to determine the next phase:

- `draft`: review the relevant requirements and ask the user to confirm the spec is authoritative for testing; then present the coverage plan at Gate 1.
- `approved`: continue from the coverage plan or test-writing phase, based on what already exists.
- `tests-approved`: await implementation, unless the user asks to continue after implementation is complete.
- `implemented`: run Phase 4.
- `mutation-checked`: report completion unless the user requests more work.

If the status contradicts what is on disk, tell the user before acting. Never infer approval from silence.

## Changing things later

- New or changed behavior: the user updates and approves the spec first, then tests are updated, then code is implemented; re-run the relevant gate.
- Test looks wrong during implementation: stop, show evidence, and let the user decide whether the spec or test is wrong. Do not inspect the implementation to reverse-engineer intended behavior.
- Bug found by manual use: describe the potential spec gap and ask the user to update or confirm the requirement. Then write a failing test from the approved requirement.

## Gotchas

- Reading the implementation "just to understand the interface" ends the tests' independence. Read declarations only.
- A test that passes on the stub is a defect, not a success.
- A mutation-driven test with no requirement behind it tests the implementation again. Raise it as a possible spec gap instead of silently adding a requirement.
- Too many questions at once makes users approve blindly. Batch and prioritize questions, and offer clearly labelled interpretations where useful.
- Do not pad the test suite: each test should verify a distinct requirement, boundary, invariant, or failure mode.
- Keep e2e tests to the few that cover real user-visible paths.

## Bundled files

`references/` holds the per-phase procedures listed above. `scripts/spec_matrix.py` checks that each requirement has tests at its declared level:

```bash
python3 <skill-dir>/scripts/spec_matrix.py --spec <spec> --tests tests
```
