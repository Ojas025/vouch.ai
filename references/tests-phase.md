# Phase 4: Write the Tests (blind)

Precondition: spec `Status: approved`. If it is `draft`, stop and go back to Gate 2.

## Blindness protocol

Work only from the spec, the interface signatures, and the project's test conventions. Do not read the implementation, any draft of it, or existing tests for this feature. If your environment supports sub-agents or fresh sessions, delegate this phase to one that receives only those inputs. If you cannot, state that plainly in the Gate 3 summary so the user can judge.

## Order and references

Read the matching reference before writing each level.

| Level | Read | Directory |
|---|---|---|
| unit: examples, edge and error cases, property tests for invariants | `unit-tests.md`, `property-tests.md` | `tests/unit/` |
| integration: real collaborators, failure paths, property tests where useful | `integration-tests.md`, `property-tests.md` | `tests/integration/` |
| e2e: very few, real entry point | `e2e-tests.md` | `tests/e2e/` |

## Common rules

- Put the requirement ID in every test name or docstring. Hyphens become underscores in identifiers (`PRC-014` becomes `PRC_014`), which keeps traceability checkable.
- Mark property-based tests with the framework's marker or a `prop` name segment so they can be filtered. No separate property directory.
- Expected values are hand-derived from the spec. Never compute them by calling the code under test or reimplementing its logic.
- No skipped, expected-fail, or disabled tests.
- If the spec is ambiguous, list the ambiguity for the Gate 3 summary and ask. Do not guess and do not edit the spec yourself.

## Traceability check

Run `python3 <skill-dir>/scripts/spec_matrix.py --spec <spec> --tests tests`. Every requirement must be present at each of its declared levels, and no ORPHAN IDs (IDs in tests but not in the spec) may remain. Then continue to `red-run-and-handoff.md`.
