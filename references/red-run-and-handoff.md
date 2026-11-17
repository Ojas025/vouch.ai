# Phase 5: Red Run, Gate 3, and Hand-off

Contents: red run steps · self-review checklist · red-run classification · Gate 3 summary · hand-off block

## Red run steps

1. If the code under test does not exist, create the smallest stub that lets tests be collected (right signatures, body raises "not implemented"). Tell the user it is a stub. If code already exists, run against it and say so.
2. Run unit, then integration, then e2e. Some may legitimately be unable to run before implementation; say which and why.
3. Classify every result using the table below, fix harness problems and vacuous tests, and re-run.
4. Complete the self-review checklist, then present the Gate 3 summary and stop.
5. On approval, set `Status: tests-approved` and give the hand-off block.


## Self-review checklist (do this before presenting)

For every test file written:

- [ ] Each test names a requirement ID that exists in the spec
- [ ] Every example row, edge case, and error case in the spec has a test at its declared level
- [ ] Every stated invariant has a property-based test, inside a unit or integration file (no separate directory)
- [ ] Every expected value traces to the spec or is an obvious hand-computed constant
- [ ] No expected value comes from calling the code under test or reimplementing it
- [ ] The unit under test is never mocked; only external boundaries are doubled
- [ ] No vacuous assertions (`is not None`, truthy, `len > 0`, "did not raise") as the only check
- [ ] No skip, expected-fail, `.only`, or ignore markers; no swallowed exceptions
- [ ] Every error test asserts the specific error or result
- [ ] Property tests state a real invariant, use a valid-domain generator, and are reproducible in CI
- [ ] Integration tests use real collaborators and cover failure paths
- [ ] E2E tests use the real entry point, no mocks of own code, and are few
- [ ] `python3 scripts/spec_matrix.py` reports no MISSING and no ORPHAN entries
- [ ] No behavior was tested that the spec does not state

## Red-run classification

| Result on the stub (or current code) | Meaning | Action |
|---|---|---|
| Fails with an assertion on behavior, or "not implemented" | Right reason | Keep |
| Fails with import/collection error, fixture typo, bad path | Wrong reason | Fix the harness, not the assertion |
| Passes | Vacuous test, or stub satisfies it | Investigate; fix or remove the test and say what you did |
| Cannot run (missing infrastructure) | Environment gap | Report what is missing; propose the smallest fix; ask before installing |

## Gate 3 summary format

```
Tests ready for review: <feature>

Files: tests/unit/<...>, tests/integration/<...>, tests/e2e/<...>
Counts: unit N (of which property M), integration N (property M), e2e N

Requirement → tests
| ID | Title | Level | Tests |
| PFX-001 | ... | unit | 4 (incl. 1 property) |
| ... |

Red run: X ran, Y failed for the right reason, Z could not run (reason).
Fixed during this phase: <harness fixes, vacuous tests corrected>

Questions / ambiguities (spec may need a decision):
1. ...

Intentionally not tested (and why): <list, or "none">
Blindness: tests were written by <a fresh sub-agent / this session> without reading the implementation. <state any limitation>

Awaiting your review. I won't hand off for implementation until you approve.
```

Be honest in the Blindness line. If you saw implementation bodies at any point, say so; the user may want the tests regenerated in a fresh session.

## Hand-off block (give verbatim to whoever implements)

```
Tests in tests/unit, tests/integration, tests/e2e are READ-ONLY for implementation.
Make them pass by changing source code only. Never edit, skip, loosen, or bypass a test,
and never special-case test inputs or detect a test environment.
If a test looks wrong or contradicts the spec: STOP and report with evidence.
The spec gets updated first, then the test, with user approval.
When finished, report what you verified and what you could not verify.
An honest "cannot pass this without cheating" is preferred over a green hack.
```

Remind the user that instructions can be bypassed. If the implementer is an agent, enforce read-only access with sandbox permissions, a pre-edit hook, or CODEOWNERS on the test directories.
