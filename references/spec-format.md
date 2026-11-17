# Spec Format (Phase 3)

Contents: file layout · header · decision log · requirement entry · field rules · self-lint · worked example

## File layout

One file per feature (`specs/<feature-slug>.md`), or one section per feature if the repo already keeps a single spec document. Use `assets/spec-template.md` as the starting skeleton.

```
# Spec: <Feature name>          (header block)
## Purpose
## Non-goals
## Decision log
## Requirements
### PFX-001 <title>
### PFX-002 <title>
...
```

## Header block

| Field | Meaning |
|---|---|
| Prefix | 2–8 uppercase letters/digits, unique per feature (e.g., `PRC`). IDs are `PFX-001`, `PFX-002`, … never renumbered or reused |
| Status | `draft` → `approved` → `tests-approved` → `implemented` → `mutation-checked`. Record waivers: `approved (gate 1 waived by user)` |
| Interface | Public signatures only (names, parameters, types, return types, endpoints, CLI flags) |
| Test locations | Directories used for unit, integration, e2e |

## Decision log

One line per decision, including rejected ideas and their reasons. IDs `D-01`, `D-02`, …

```
D-01 Negative prices: rejected, raise an error. Reason: refunds are modeled elsewhere.
D-02 Whitespace around input: trimmed. Reason: form fields often include it.
D-03 More than two decimals: error, no rounding. Reason: silent rounding hides user mistakes.
```

## Requirement entry (use exactly this shape)

```
### PFX-001 <short title, one behavior>
**Behavior:** 1–2 sentences stated so each can be asserted.
**Level:** unit | integration | e2e (one or more, comma-separated)
**Source:** user | agent-proposed (approved)
**Examples:**
| # | Given / Input | Expected |
|---|---|---|
| 1 | ... | ... |
**Invariants:** property statements, or "none"
**Errors and edge cases:** each case with its exact expected error or result, or "none"
**Decisions:** D-01, D-03 (or "none")
```

## Field rules

- **One behavior per requirement.** If the title needs "and", split it.
- **Examples are concrete and hand-derived.** Pick values from the user's words or compute them by hand. Never copy from a running implementation. Include at least one boundary example when the behavior has a range.
- **Invariants become property-based tests.** Write them as statements about all valid inputs ("output length equals input length", "parse(format(x)) == x for x in [0, 10^9]"). State the input domain. If none can be stated honestly, write "none"; do not invent.
- **Errors are specific.** Name the error type or result. State message content only if the user decided it matters.
- **Level decides the directory.** `unit`: logic in isolation. `integration`: behavior across real collaborators (database, filesystem, other modules). `e2e`: a user-visible path through the real entry point. A requirement may list more than one level when it needs both (e.g., the rule is unit-tested and its wiring is integration-tested).
- **No implementation detail.** Say what, not how. Do not name internal functions or data structures unless they are part of the public interface.
- **No weasel words.** "Handles", "appropriately", "gracefully", "fast" are untestable until replaced by examples or numbers.

## Self-lint (run before Gate 2)

For each requirement:
- [ ] Has ID, behavior, level, source, examples, invariants, errors/edge cases, decisions fields
- [ ] Every sentence can be turned into an assertion
- [ ] At least one concrete example; boundary example where a range exists
- [ ] Error cases state the exact expected error or result
- [ ] Two engineers would implement the same behavior from it (otherwise add an example)
- [ ] Provenance is accurate; no agent-proposed item the user did not approve

For the document:
- [ ] All decisions from the interview are in the log, including rejected ones
- [ ] Non-goals listed
- [ ] No open or deferred items remain
- [ ] IDs are unique and in order
- [ ] Interface section contains signatures only

## Worked example (neutral feature: `slugify`)

```
### SLG-003 Collapse repeated separators
**Behavior:** Runs of spaces, hyphens, or underscores in the input become a single hyphen in the output.
**Level:** unit
**Source:** user
**Examples:**
| # | Input | Expected |
|---|---|---|
| 1 | "a  b" | "a-b" |
| 2 | "a - _ b" | "a-b" |
| 3 | "a" | "a" |
**Invariants:** output never contains two consecutive hyphens, for any input string.
**Errors and edge cases:** "" returns ""; "---" returns "" (leading/trailing hyphens trimmed, D-02).
**Decisions:** D-02
```

## Gate 2 procedure

After the self-lint passes, set `Status: draft`. Present the file path, the count of requirements per level, and anything you were unsure about. End your turn and wait. On explicit approval, set `Status: approved`. Do not start tests while the status is `draft`.
