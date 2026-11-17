# Writing Unit Tests (Phase 4, level: unit)

Contents: scope · derivation from the spec · structure and naming · assertion rules · doubles · determinism · anti-patterns · idioms

## Scope

A unit test exercises one unit (function, class, small module) in isolation. Collaborators that cross a system boundary are replaced with test doubles. The unit under test is never replaced.

Directory: `tests/unit/` (or the repo's existing equivalent). One test file per unit or per spec feature, whichever the repo already does.

## Derive tests from the spec, line by line

For each requirement at level `unit`:

| Spec field | Test(s) to write |
|---|---|
| Examples table | One test per row (parameterized when the framework supports it), expected value copied from the spec |
| Boundary examples | Same, with the boundary named in the test name |
| Errors and edge cases | One test per case, asserting the specific error type or result |
| Invariants | A property-based test; see `property-tests.md` |
| Behavior sentence not covered above | Add an example test, and note the gap in the Gate 3 summary (the spec should probably get an example) |

Do not add tests for behavior the spec does not state. If you think something is missing, raise it in the Gate 3 summary; do not invent a requirement.

## Structure and naming

- One behavior per test. A failure should point to one broken rule.
- Name = ID + behavior: `test_PRC_014_empty_string_raises`, `PRC_014 rejects empty string`.
- Arrange / act / assert, visibly separated. No logic (loops, conditionals) in the test body beyond what a parameterized table needs.
- Small, local fixtures. Prefer inline data over shared mutable fixtures.
- Parameterize example tables so each row reports separately.

## Assertion rules

- Expected values come from the spec or are hand-computed constants. Never call the unit (or a reimplementation of it) to produce the expectation.
- Assert exact values. Use exact equality; for floats, a tolerance stated in the spec.
- Errors: assert the specific type. Assert message content only if the spec states it.
- No vacuous assertions as the sole check: `is not None`, truthy, `len(x) > 0`, `assert True`, "did not raise".
- Assert outcomes, not internal calls. Verifying that a private helper was called couples the test to the implementation.
- Never wrap the assertion in `try/except` that swallows failure.

## Test doubles

- Double only external boundaries: network, clock, randomness, filesystem when it matters, third-party services.
- Never mock the unit under test, and avoid mocking its pure in-process collaborators; use the real ones.
- A mock configured to return the answer the test then asserts proves nothing. If a double's return value is the expectation, rewrite.

## Determinism

- Freeze or inject time, seed or inject randomness, avoid sleeps, avoid ordering assumptions between tests.
- Tests must pass in any order and in isolation.

## Anti-patterns (reject on sight)

| Pattern | Why it fails |
|---|---|
| Expected value computed by the code under test | Tautology |
| Snapshot of current output, regenerated freely | Records bugs as truth |
| `skip`, `xfail`, `.only`, `@Ignore` | Hides gaps |
| Many unrelated assertions in one test | Failure is ambiguous |
| Test of a mock | Verifies the double, not the unit |
| Hard-coded values that happen to match a special case in the code | Easily gamed; prefer a second, different example or a property |

## Idioms by ecosystem (adapt to the repo's runner)

- Python / pytest: `@pytest.mark.parametrize`, `pytest.raises(Type)`; Hypothesis for properties.
- JavaScript / TypeScript: `test.each`, `expect(fn).toThrow(Type)`; fast-check for properties.
- Java / Kotlin: parameterized tests, `assertThrows`; jqwik for properties.
- Go: table-driven tests with `t.Run`; native fuzzing or `rapid` for properties.
- C++: parameterized tests (`TEST_P`) or Catch2 generators; RapidCheck for properties.
- Rust: `#[test]` with table loops or `rstest`; `proptest` for properties.
