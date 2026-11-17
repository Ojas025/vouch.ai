# Spec: <Feature name>

- **Prefix:** `<PFX>`
- **Status:** `draft` | `in-review` | `ready-for-implementation`
- **Test locations:** unit: `tests/unit/`, integration: `tests/integration/`, e2e: `tests/e2e/`

## Purpose

<What problem does this feature solve, and what outcome is expected?>

## Non-goals

- <What this feature deliberately does not do.>

## Requirements

### `<PFX>-001` <One observable behavior>

- **Behavior:** <Precise, testable requirement.>
- **Approval:** `approved` | `agent-proposed`
- **Verification level:** `unit` | `integration` | `e2e` | `analysis` | `inspection`
- **Invariants:** <What must always remain true, if applicable.>
- **Errors and edge cases:** <Relevant boundaries and failure behavior, if applicable.>

| Given / Input | When | Expected result |
|---|---|---|
| <Initial condition or input> | <Action or event> | <Observable pass condition> |

<!-- Add one subsection per independently verifiable behavior. Remove fields that do not apply. -->

## Design notes (if relevant)

- **Candidate:** <Field, property, dependency, or design choice.>
- **Requirement served:** `<PFX>-001`
- **Justification:** <Why it is needed, why this representation fits, and why it is not redundant or derivable.>
- **Status:** `approved` | `agent-proposed`

## Open decisions (if any)

- **Question:** <Unresolved decision>
- **Impact:** <What could change based on the answer>
- **Blocks implementation:** `yes` | `no`

<!-- Ready for implementation only when all requirements are approved and verifiable, and no blocking decisions remain. -->
