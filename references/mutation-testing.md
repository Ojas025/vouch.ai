# Mutation Testing (Phase 6)

Trigger: the user says the implementation is done or asks for mutation testing. Steps: confirm green, run scoped, triage every survivor, add spec-derived tests (spec changes go to the user first), repeat until the done criteria are met, then set `Status: mutation-checked`.

Contents: what it is · prerequisites · tools · running it · triage · writing killing tests · thresholds and done criteria · fallback · report format

## What it is (and is not)

Mutation testing is **not a kind of test you write**. It is a measurement run over the existing tests. The tool makes many copies of the source, each with one small change (flip `<` to `<=`, swap `+` and `-`, change a constant, delete a statement, return a default), then runs the tests against each copy.

- **Killed**: at least one test failed, so the tests noticed the change.
- **Survived**: all tests passed, so the tests cannot tell this broken version from the correct one.
- **Equivalent**: the change has no observable effect; excluded from the score.

`mutation score = killed / (total − equivalent)`

Coverage says a line ran. Mutation score says a test would notice if it were wrong. Suites written by agents often have high coverage with a weak mutation score.

## Prerequisites

- All tests for the feature are green. If not, report failures and stop; do not edit tests to get green.
- Tests are locked (read-only for implementation). Do not let the implementer see mutation output as a target to game.
- Source files for the feature are identified (from the spec's interface and the repo).

## Tools (use the repo's if present; ask before installing)

| Ecosystem | Tools |
|---|---|
| Python | mutmut, cosmic-ray |
| JS / TS / C# / Scala | Stryker |
| Java / Kotlin | PIT (Pitest) |
| Go | go-mutesting, gremlins |
| C / C++ | Mull |
| Rust | cargo-mutants |

Check the tool's current documentation for exact flags and configuration; they change between versions.

## Running it

1. Scope to this feature's source files and its **unit tests** (fast). Integration tests only if the user asks and time allows; they are much slower.
2. Exclude generated code, logging statements, and other lines the tool cannot meaningfully mutate.
3. Set a time budget. If the run is too slow, narrow the file set or use the tool's incremental mode instead of dropping the run.
4. Save the report (killed, survived, timeout, equivalent) to the project's usual report location, not into the test directories.

## Triage every survivor

Do not skim. Classify each survivor and record the classification.

| Type | Meaning | Action |
|---|---|---|
| **Real gap** | Spec requires the behavior; no test checks it | Write a spec-derived test that cites the requirement ID |
| **Spec gap** | The changed behavior is not mentioned in the spec | Take it to the user: is it required? If yes, new requirement (approved) then a test; if no, add to non-goals |
| **Equivalent mutant** | No observable behavior change | Mark equivalent with a one-line reason |
| **Dead or unused code** | The mutated code does nothing | Tell the user; they decide whether to remove it |
| **Timeout** | Mutation caused an infinite loop or hang | Usually counts as killed by the tool; confirm and note |

## Writing killing tests

Rules:
- Every new test traces to a requirement ID. A test written only to kill a mutant, with no requirement behind it, tests the implementation again. Bring it to the user as a spec gap instead.
- New tests follow the same rules as Phase 4 (hand-derived expected values, no mocks of the unit, no vacuous assertions) and live in the existing unit or integration files, with property tests marked as before.
- Do not modify existing tests, and do not change source code to make mutants disappear, unless the user instructs it.
- Any spec change goes back through the user (Gate 2 style) before new tests are written for it.

Loop: run → triage → add or propose tests → rerun scoped to the files involved.

## Thresholds and done criteria

Measure first, then agree on a floor with the user for this feature. Typical practice: about 60% minimum, 80% or more for core or critical logic. Do not chase 100%: equivalent mutants and diminishing returns make it expensive.

Done when:
- [ ] Every survivor is classified; none is "unexplained"
- [ ] No survivor in behavior the spec requires remains unaddressed
- [ ] Score meets the agreed floor
- [ ] New tests all cite requirement IDs; the matrix script reports no MISSING or ORPHAN
- [ ] Spec updated for any approved new requirement or non-goal
- [ ] Status set to `mutation-checked`

## Fallback when no tool is available

Manual sabotage: break the feature's source in about 10 targeted ways, one at a time (return a constant or empty value, flip a comparison, off-by-one a bound, negate a condition, delete a branch, drop a validation), run the tests after each, and record whether they fail. Revert each change. Ask the user before doing this on their working tree; prefer a scratch copy or a throwaway branch.

## Report format

```
Mutation report: <feature>
Tool: <name/version>   Scope: <files>   Tests: unit (integration: no)
Total N · Killed K · Survived S · Equivalent E · Timeout T · Score X%

Survivors
| # | File:line | Change | Class | Action |
| 1 | ... | `<` → `<=` | Real gap | Added test PFX_012_... |
| 2 | ... | `+1` removed | Spec gap | Question for you: ... |

Questions for you: ...
Done criteria: <met / not met, with what is left>
```
