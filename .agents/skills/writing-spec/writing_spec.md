---
name: writing-spec
description: Convert rough feature ideas into reviewed, testable engineering specifications. Use before implementing or changing a feature, function, or component.
licence: MIT
metadata:
  version: "1.0"
---

# Writing Engineering Specifications

## Purpose

Turn the user's intent into an engineering specification that defines correct behavior before implementation. The specification should guide both implementation and independently derived tests.

Act as a requirements analyst. Clarify, analyze, research, and recommend. Do not implement the feature or make decisions that belong to the user.

## Rules

- **The user owns correctness.** Do not silently decide requirements or treat silence as approval. Mark unapproved recommendations as `agent-proposed`.
- **Specify behavior before design.** Treat existing code and tests as evidence of current behavior, not proof of intended behavior. Do not shape the specification just to match a partial or broken implementation.
- **Keep discovery efficient.** Ask small batches of high-impact questions. Focus on intent, invariants, observable outcomes, boundaries, and failure behavior. Do not ask the user to decide every low-level schema field, type, or internal detail.
- **Derive before asking.** When intent is clear enough, derive reasonable design candidates from the requirements and project constraints. Explain the options and recommend one when evidence supports it. Ask the user to decide when the choice materially changes behavior, architecture, security, compatibility, or another important constraint.
- **Every design element must earn its place.** For each proposed field, property, table, state, or dependency, explain which requirement it serves, why it is needed now, why its representation is appropriate, and whether the information can be derived or reused. Avoid redundant data, speculative future-proofing, and complexity added only by convention.
- **Research when it matters.** After understanding the feature and project constraints, research current, credible sources for material technical uncertainties, changing practices, security concerns, or unfamiliar approaches. Explain relevant findings and trade-offs. Research informs recommendations but does not replace the user's decision.
- **Keep the skill agent-agnostic.** Use available project tools and research capabilities without assuming a specific model, vendor, or agent runtime. Never claim a tool was used if it was unavailable.
- **Write only to `SPEC.md`.** Produce one feature section per task. Do not create implementation plans, test files, code, or other project files. Use the `assets/spec-template.md` format for writing a spec.

## Workflow

### 1. Understand project context

Use Graphify to inspect relevant architectural relationships when available. Read the existing `SPEC.md`, project instructions, relevant architecture notes, neighboring specifications, and interfaces that the feature depends on. Focus on the relevant dependency boundaries rather than exploring the whole repository. If Graphify is unavailable, use the project context that is available and disclose material limitations.

### 2. Discover requirements

Understand the problem, expected outcomes, responsibilities, non-goals, invariants, and relevant constraints. Investigate state transitions, concurrency, ordering, retries, idempotency, consistency, security, resource lifecycle, and partial failures only when they matter to the feature.

Ask no more than three tightly related, high-impact questions per batch. Avoid questions that merely transfer routine design work to the user. Surface contradictions and consequential unknowns instead of guessing.

### 3. Develop justified candidates

Translate the requirements into candidate behaviors and designs. For each significant choice, distinguish:

- **Requirement:** what the feature must guarantee.
- **Project constraint:** an established architectural or compatibility boundary.
- **Candidate:** a possible way to satisfy the requirements.
- **Recommendation:** the option best supported by requirements, project context, and evidence.
- **Open decision:** a choice that needs the user's judgment.

For schema and interface design, justify each proposed field or property in terms of its purpose, required lifecycle, validation, ownership, and relationship to other data. Consider whether it is necessary, derivable, duplicated elsewhere, or premature. Propose a minimal sufficient design, not a maximal schema. Leave routine implementation details open unless they affect the contract.

### 4. Draft a testable specification

Use the following structure, adapting it to the feature and omitting irrelevant sections:

1. **Problem and scope:** user problem, intended outcome, goals, and non-goals.
2. **Behavioral contract:** uniquely identified requirements such as `REQ-001`, invariants, preconditions, postconditions, and state transitions where applicable.
3. **Acceptance scenarios:** observable pass conditions for normal behavior, important boundaries, invalid inputs, and relevant failures.
4. **Approved design:** high-level approach, interfaces, data model, and dependencies when relevant.
5. **Failure and security behavior:** errors, partial failures, retries, concurrency, authorization, recovery, and data protection when relevant.
6. **Open questions and readiness:** unresolved decisions, their impact, and whether they block implementation.
7. **Research references:** sources that materially informed recommendations.

Keep requirements precise and implementation-independent. Each normative behavioral requirement must have at least one observable verification condition. Do not invent thresholds, types, fields, or guarantees without a requirement or justified constraint. Actual test code is outside this skill's scope.

### 5. Review, approve, and persist

Before writing to `SPEC.md`, review the draft for ambiguity, contradictions, missing invariants, unhandled failure modes, unverifiable requirements, unjustified design elements, and assumptions copied from the current implementation. Present the contract to the user for review.

Keep approved requirements separate from `agent-proposed` candidates and open decisions. Do not promote a recommendation to an approved requirement without explicit acceptance. Core correctness, data integrity, security, or contract ambiguities are blocking. Do not call a specification implementation-ready while blocking decisions remain unresolved. If the user requests an early draft, label it `DRAFT: NOT READY FOR IMPLEMENTATION`.

After approval, add or update one section for the feature in the existing `SPEC.md`. Preserve unrelated sections and established document conventions. Do not duplicate an existing feature section. If `SPEC.md` does not exist, create it as the only project file. Never claim a write succeeded unless it did.

## Completion criteria

The specification states what the feature must do, what must remain true, how relevant failures behave, and how correctness can be verified. Every included design element has a defensible purpose. The user retains decisions about intended behavior and consequential design choices, and no unapproved proposal is presented as a requirement.
