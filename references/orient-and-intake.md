# Phase 0 (Orient) and Phase 1 (Intake)

## Phase 0: Orient

1. Read project configuration only: dependency files, test config, CI files, existing test layout. Detect language, test runner, property-based library, and mutation tool. Do **not** open the feature's implementation.
2. Locate the spec home. If the repo already has a spec document, add a section there. Otherwise create `specs/<feature-slug>.md` from `assets/spec-template.md`.
3. Default test paths: `tests/unit/`, `tests/integration/`, `tests/e2e/`. Follow the repo's existing layout if it differs, and mention it in one line.
4. Interface signatures: ask the user for them. If they point to a file, read **declarations only** (names, parameters, types, return types) and ignore bodies and behavior-describing comments. If bodies cannot be avoided, say so, and recommend running the tests phase in a fresh session or sub-agent that receives only the spec and signatures.
5. Check that `scripts/spec_matrix.py` (in this skill's folder) runs: `python3 <skill-dir>/scripts/spec_matrix.py --help`. If Python is unavailable, do the requirement-to-test check by hand with a text search for each ID.

Ask a question in this phase only if genuinely blocked. State detected defaults in one line instead.

## Phase 1: Intake

Inputs: a feature name and a natural-language description (rough is fine). If either is missing, ask for it in one message. Optional: interface signatures, an existing spec to extend.

Restate the description as a short numbered list of **stated behaviors** (S1, S2, ...), using the user's own wording as far as possible. This keeps their intent visible and lets you later mark what came from them versus from you. Then go straight to Phase 2.
