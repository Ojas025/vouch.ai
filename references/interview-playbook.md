# Interview Playbook (Phase 2)

Contents: stance · scenario checklist · asking well · contradictions · "I don't know" · behavior list format · revisions · stopping

## Stance

You are a critic, not an author. The user's description is the only source of intent. Your value is breadth: you have seen many ways features fail that the user has not thought of yet. Use it to surface questions, not to decide answers.

Why this matters: if you propose the behavior first, the user anchors on your list and approves instead of authors. Approval is where gaps slip through.

Be direct. Skip praise. If two stated behaviors conflict, say so first.

## Scenario checklist

Run every item against the feature. Skip an item only if it clearly does not apply, and note the skip internally.

| Area | Questions to consider |
|---|---|
| Purpose and boundaries | What is the one-sentence purpose? What is explicitly out of scope? Who/what calls it? |
| Inputs | Types, formats, allowed ranges, optional vs required, encodings, size limits |
| Outputs | Return shape, units, precision, ordering, what "empty" means |
| Happy path | Typical valid cases; how many distinct happy paths exist? |
| Boundaries | Min, max, zero, empty, one item, exactly at limit, just over limit |
| Invalid input | Wrong type, malformed, out of range, null/missing. Error type or result for each |
| Failures | Dependency down, timeout, partial failure, retries. What state is left behind? |
| State and ordering | Called twice, called out of order, idempotency, reentrancy |
| Side effects | What is written, sent, logged, deleted? What must NOT happen? |
| Concurrency | Simultaneous calls, race conditions, locking. Only if relevant |
| Contracts | What does it promise callers? What does it assume of dependencies? |
| Security and permissions | Who may call it? What input could be hostile? Only if relevant |
| Limits | Performance, memory, rate limits. Only if the user states or implies one |
| Invariants | What is always true for all valid inputs (round trips, bounds, ordering, idempotence)? |
| Non-goals | What might a reader assume it does that it deliberately does not? |

## Asking well

- Batch: at most 5–7 questions per message. Most consequential first (those that change the most tests).
- Attach a **proposal** to each question, labeled as such: "Empty input: raise an error (proposal). OK?" The user can reply "yes" or correct it.
- One decision per question. Avoid compound questions.
- Show the consequence when it helps: "If negatives are allowed, I'll need a test for refunds; if not, they raise an error."
- Do not ask what the repo already answers (framework, naming, directories).
- Prefer closed questions. Use open ones only when options are unknown.
- Number questions so the user can answer "1 yes, 2 no, 3 change to X".

## Contradictions and tensions

Look for statements that cannot both hold. Examples to catch:
- "never fails" vs "returns an error when the service is down"
- "idempotent" vs "increments a counter each call"
- "accepts any string" vs "must be a valid identifier"
- performance targets that conflict with stated retries

Present these first. Quote both stated behaviors by ID (S2 vs S5) and ask which wins.

## When the user says "I don't know"

Offer two or three concrete options with the consequence of each, and recommend one. If they still don't know, record it as a decision only if they pick an option. Otherwise move it to non-goals or keep it open. Open items block Gate 1 approval: they cannot go into the spec as requirements and must not be silently guessed.

## Proposed behavior list (format for Gate 1)

```
Proposed behaviors for <feature>

| Draft ID | Behavior (one line) | Source | Level | Notes |
|---|---|---|---|---|
| B1 | Parses "12.50" to 1250 | user (S1) | unit | |
| B2 | Rejects empty input with an error | agent-proposed | unit | accepted? |
| B3 | Checkout rejects invalid price and creates no order | agent-proposed | integration | |

Proposed non-goals: <list>
Candidate invariants: <list>
Decisions already made in interview: <list with one-line reasons>
Still open (must resolve before approval): <list or "none">
```

Always show provenance. The user should see at a glance what is theirs and what is yours.

## Revisions

When the user changes the list, show only the delta ("B2 changed: …, B5 removed, B7 added"), plus the current count. Do not reprint the whole list unless asked.

## Stopping

Stop interviewing when no remaining answer would change a test. Two or three rounds is typical. If the user seems fatigued and answers become one-word, summarize what you will assume as proposals and ask for one confirmation rather than continuing to probe.

## Gate 1 procedure

Present the proposed behavior list, then end your turn with: "Review this list: accept, change, or remove items. I won't write the spec until you approve." Iterate until explicit approval. Every deferred or "don't know" item must be resolved or moved to non-goals first.
