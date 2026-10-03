# Vouch: Multi-Modal Forensic Investigation Engine
## Architecture Specification (v3.5 — Evidence-Ledger, Multi-Dimensional Decision)

> **Status: specification.** This document describes the intended architecture. Performance
> figures are marked **[measured]** (reproduced by a script) or **[target]** (asserted,
> unverified).

---

## 1. Core Principle

```
RAW DATA → OBSERVATION → CLAIM → DECISION → NARRATIVE
```

Never:

```
RAW DATA → LLM OPINION → DECISION
```

This distinction is what makes the system auditable, evaluable, and debuggable. LLMs are
investigators operating over evidence — they are not the evidence system itself.

---

## 2. Architecture Diagram

```
Telegram / WhatsApp / REST
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ EDGE / INTAKE                                                               │
│                                                                             │
│  • Coarse IP / connection limiting (before auth)                            │
│  • Body-size limiting                                                       │
│  • Raw-body capture (object storage for large payloads)                     │
│  • Channel-specific authentication                                          │
│  • Replay / idempotency checks                                              │
│  • Authenticated sender / account rate limiting                             │
└─────────────────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ POSTGRES DURABLE ACCEPT                                                     │
│                                                                             │
│  • WebhookDelivery (one webhook request)                                   │
│  • ExternalEvent(s) (one event within a delivery)                           │
│  • AuditEvent                                                               │
│  • OutboxEvent (transactional outbox)                                       │
└─────────────────────────────────────────────────────────────────────────────┘
           │
           ▼
      Outbox Relay → Redis Stream (vouch:ingest)
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ CANONICALIZATION & ENTITY EXTRACTION (code, no LLM)                         │
│                                                                             │
│  Multiple representations:                                                  │
│  • raw_text                                                                 │
│  • nfkc_text                                                                │
│  • format_stripped_text                                                     │
│  • confusable_skeleton (UTS #39 — feature, NOT replacement)                 │
│  • script_profile / mixed_script_flags                                      │
│  • entity_sidecar (typed entities)                                         │
└─────────────────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ TRIAGE / CACHE HIERARCHY                                                    │
│                                                                             │
│  Tier 0: exact hashes / exact normalized entities                           │
│  Tier 1: MinHash / structural similarity (candidate generator only)         │
│  Tier 2: deterministic entity graph (retrieval evidence, not proof)         │
│  Tier 3: dense retrieval (deferred)                                         │
│  Tier 4: expensive modality analysis                                        │
│  Tier 5: LLM investigation (last, most expensive)                           │
│                                                                             │
│  Prior Evidence Re-Validator (first-class service)                         │
└─────────────────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ MODALITY BRANCHES (hardened artifact sandbox)                               │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │ 4A URL       │  │ 4B APK       │  │ 4C DOC       │  │ 4D IMAGE     │    │
│  │ Obscura      │  │ Multi-modal: │  │ oletools/    │  │ OCR/QR/      │    │
│  │ (stealth)    │  │ metadata ·   │  │ pikepdf      │  │ C2PA         │    │
│  │ Playwright   │  │ permissions ·│  │              │  │              │    │
│  │ API          │  │ icons ·      │  │              │  │              │    │
│  │              │  │ content ·    │  │              │  │              │    │
│  │              │  │ certificates │  │              │  │              │    │
│  │              │  │ + delegated  │  │              │  │              │    │
│  │              │  │ dynamic      │  │              │  │              │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                 │                 │                 │            │
│         └────────────┬────┴────────┬────────┴────────┬────────┘            │
│                      ▼             ▼                 ▼                      │
│              EvidenceBundle   EvidenceBundle   EvidenceBundle  EvidenceBundle│
│                                                                             │
│  All browser/parser output is UNTRUSTED DATA.                               │
└─────────────────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ FACT SERVICES (shared code, TTL-cached, no LLM)                             │
│                                                                             │
│  • Threat feeds: URLhaus, ThreatFox, PhishTank, OpenPhish, GSB              │
│  • Registry: RDAP domain age, allowlists                                   │
│  • Resolution: IFSC/Razorpay, number-class                                  │
│  • Output: SourceFact records → Evidence Ledger                             │
└─────────────────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ IMMUTABLE EVIDENCE LEDGER                                                    │
│                                                                             │
│  • Evidence (append-only, typed, provenance-tracked)                        │
│  • Claims (typed, evidence-referenced)                                     │
│  • Temporal: observed_at, valid_from, valid_until, source_version           │
│  • Investigation Snapshot (immutable, versioned)                           │
└─────────────────────────────────────────────────────────────────────────────┘
           │
           ▼
     Investigation Snapshot
           │
     ┌─────────┼─────────┐
     ▼         ▼         ▼
  Analyst A  Analyst B  Auditor
  (indep.)   (indep.)   (gated)
     │         │         │
     └─────────┼─────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ DETERMINISTIC POLICY ENGINE                                                 │
│                                                                             │
│  • Claim coverage                                                           │
│  • Contradictions                                                           │
│  • Evidence freshness                                                       │
│  • Required evidence policy                                                 │
│  • Multi-dimensional threat classes                                         │
└─────────────────────────────────────────────────────────────────────────────┘
           │
           ▼
     DecisionRecord
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ GROUNDED RESPONSE                                                           │
│                                                                             │
│  • Claim-ID anchored prose                                                  │
│  • Structural grounding validation                                          │
│  • Fail-closed templates                                                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Ingestion Layer

### 3.1 Data Model

**WebhookDelivery** — one webhook request from a channel:

| Field | Description |
|---|---|
| `id` | Internal delivery ID |
| `channel_account_id` | Channel account identifier |
| `provider_delivery_id` | Provider's delivery ID (if exposed) |
| `raw_body_sha256` | SHA-256 of raw body |
| `signature_verified` | Whether channel auth succeeded |
| `received_at` | Receipt timestamp |
| `raw_blob_ref` | Object storage reference for large payloads |

**ExternalEvent** — one event within a delivery:

| Field | Description |
|---|---|
| `id` | Internal event ID |
| `delivery_id` | Parent delivery |
| `event_kind` | Message, status, etc. |
| `external_event_id` | Provider's event ID |
| `sender_id` | Sender identifier |
| `object_id` | Object identifier |
| `occurred_at` | Event timestamp |
| `payload_pointer` | Reference to event payload |

Idempotency: `UNIQUE(channel_account_id, event_kind, external_event_id)`

### 3.2 Rate Limiting

Two layers, applied in order:

1. **Coarse IP / connection limit** — before authentication. Bounds unauthenticated cost.
2. **Authenticated sender / account limit** — after authentication. Bounds per-sender cost.

### 3.3 Channel-Specific Authentication

| Channel | Mechanism |
|---|---|
| Meta / WhatsApp | `X-Hub-Signature-256` (HMAC over raw body) |
| Telegram | `X-Telegram-Bot-Api-Secret-Token` (static secret) |
| REST | `X-Vouch-Id` + `X-Vouch-Timestamp` + `X-Vouch-Signature` |

REST signature: `version || timestamp || request_id || raw_body`

### 3.4 Replay Protection

| Channel | Mechanism |
|---|---|
| Meta | Persistent event-id dedup |
| Telegram | `update_id` dedup (monotonically increasing) |
| REST | Timestamp + nonce/request_id + persistent dedup |

### 3.5 Transactional Outbox

```
BEGIN
  INSERT webhook_delivery
  INSERT external_events
  INSERT outbox_event
COMMIT
       ↓
outbox relay → XADD vouch:ingest
```

The relay may publish duplicates; consumers remain idempotent.

### 3.6 Processing State Machine

```
RECEIVED → QUEUED → PROCESSING → TRIAGED → INVESTIGATING → DECIDED → RESPONDED
                                                    ↓
                                            INSUFFICIENT_EVIDENCE
```

Each worker attempt carries: `worker_id`, `attempt_id`, `attempt_no`, `lease_until`,
`started_at`, `completed_at`.

---

## 4. Canonicalization & Entity Extraction

### 4.1 Multiple Representations

Store all of:

| Representation | Purpose |
|---|---|
| `raw_text` | Original input |
| `nfkc_text` | Compatibility-folded |
| `format_stripped_text` | Cf/Cc removed |
| `confusable_skeleton` | UTS #39 skeleton (feature, NOT replacement) |
| `script_profile` | Script distribution |
| `mixed_script_flags` | Mixed-script detection |
| `entity_sidecar` | Typed entities |

**Never** transform `раypal` → `paypal` and call it canonical. UTS #39 skeletons are intermediate
processing forms for comparison, not display/storage/transmission data.

### 4.2 Typed Entities

| Field | Description |
|---|---|
| `type` | PHONE, UPI_VPA, DOMAIN, IFSC, AMOUNT |
| `raw_value` | As received |
| `normalized_value` | Canonical form |
| `span` | Position in text |
| `extractor` | Which extractor produced it |
| `extractor_version` | Version of extractor |
| `confidence` | Extractor confidence |

---

## 5. Triage & Cache Hierarchy

### 5.1 Tier Ordering

| Tier | Mechanism | Role |
|---|---|---|
| 0 | Exact hashes / exact normalized entities | Duplicate detection |
| 1 | MinHash / structural similarity | Candidate generator only |
| 2 | Deterministic entity graph | Retrieval evidence, not proof |
| 3 | Dense retrieval | Deferred |
| 4 | Expensive modality analysis | On-demand |
| 5 | LLM investigation | Last, most expensive |

MinHash is a **candidate generator**, never a duplicate detector. Every hit requires exact
similarity verification.

### 5.2 Prior Evidence Re-Validator

First-class service that answers:

- Is the prior evidence still valid?
- Which source produced it?
- What exactly did that source establish?
- When was it observed?
- Has it expired? Been retracted?
- Does it apply to the exact entity or only a related entity?
- Was it observed under the same threat class?

---

## 6. Modality Branches

All branches run in a **hardened artifact sandbox**:

- CPU / memory / wall-clock limits
- Output-size limit
- Recursion / decompression limit
- Ephemeral filesystem
- No secrets, no host mounts
- Restricted network
- Process isolation

### 6.1 URL Branch (4A)

- **Browser:** Obscura (stealth mode) via Playwright API
- **SSRF defenses:** DNS resolve → validate IP → connect for every redirect
- **Isolation:** No cookies, no logged-in profile, no cloud credentials, no host filesystem
- **Output:** All browser output is untrusted data

### 6.2 APK Branch (4B)

Multi-modal analysis:

| Modality | Analysis |
|---|---|
| Package metadata | App name, activities, package name |
| Permissions | Sensitive permission detection |
| Icons | Similarity to known fraudulent icons |
| Content | Text content within the app |
| Certificates | Authenticity and validity |
| Dynamic | Delegated hosted sandbox (separate trust level) |

### 6.3 Document Branch (4C)

- **oletools:** VBA extraction, macro detection, obfuscation detection
- **pikepdf / pypdf:** Metadata, structure, incremental updates
- Parser execution in artifact sandbox

### 6.4 Image Branch (4D)

- **OCR:** Approximate, fuzzy-match only
- **QR decode:** UPI/URL extraction
- **C2PA:** Positive-only provenance signal

---

## 7. Fact Services

### 7.1 SourceFact Schema

| Field | Description |
|---|---|
| `source` | URLHAUS, THREATFOX, PHISHTANK, OPENPHISH, GSB |
| `indicator` | URL, DOMAIN, PHONE, etc. |
| `classification` | MALWARE_DISTRIBUTION, PHISHING, C2, etc. |
| `observed_at` | When observed |
| `source_confidence` | Feed confidence |
| `expires_at` | TTL |

### 7.2 Feed Sources

| Source | Covers | Threat Class |
|---|---|---|
| URLhaus | Malware distribution | MALWARE |
| ThreatFox | C2 / IOC malware | MALWARE |
| PhishTank | Verified phishing | PHISHING |
| OpenPhish | Phishing (brand-targeted) | PHISHING |
| Google Safe Browsing | Social engineering + malware | PHISHING, MALWARE |

**Commercial use:** Google Safe Browsing is for non-commercial use. Commercial use requires
Web Risk or appropriately licensed intelligence.

---

## 8. Evidence Ledger

### 8.1 Flow

```
Fact Services
       ↓
SourceFact (typed, provenance-tracked, TTL-cached)
       ↓
Evidence Ledger (append-only)
       ↓
Investigation Snapshot (immutable, versioned)
       ↓
Agents read snapshot, propose claims
```

The Evidence Ledger is the **single authoritative state model**. There is no separate
"blackboard" — the ledger *is* the shared state. Agents never rewrite evidence; they only
propose claims that reference it.

### 8.2 Typed Claims

| Field | Description |
|---|---|
| `claim_id` | Unique claim ID |
| `investigation_id` | Parent investigation |
| `subject` | Claim subject |
| `predicate` | Claim predicate |
| `object` | Claim object |
| `scope` | Claim scope |
| `claim_type` | Type of claim |
| `evidence_refs[]` | Referenced evidence |
| `observed_at` | When observed |
| `expires_at` | TTL |
| `producer_type` | Agent / service |
| `producer_id` | Producer identifier |
| `model_id` | Model identifier |
| `prompt_version` | Prompt version |

### 8.3 Temporal Evidence

Every fact has:

- `observed_at`
- `valid_from`
- `valid_until`
- `retrieved_at`
- `source_version`

---

## 9. LLM Hub

### 9.1 Agent Roles

| Role | What it does |
|---|---|
| Orchestrator (Jev) | Plans, dispatches, budgets, converges |
| Adversarial Agent ×2 | Independent hypothesis analysts (test risk vs. benign alternatives) |
| Web Search Agent | Isolated, returns typed observations |
| CSD/SMD Detector | SMD: cheap, every submission. CSD: thread-only, rolling summary |
| Auditor | Gated set-level consistency read |

### 9.2 Agent Identity & Capabilities

Every agent invocation carries:

- `agent_id`, `investigation_id`, `run_id`, `role`
- `allowed_tools[]`, `allowed_evidence_scopes[]`
- `max_cost`, `max_runtime`, `network_policy`

### 9.3 Typed Inter-Agent Messages

| Field | Description |
|---|---|
| `sender_agent_id` | Sender |
| `receiver_agent_id` | Receiver |
| `investigation_id` | Investigation |
| `message_id` | Message ID |
| `message_type` | Type of message |
| `claims[]` | Attached claims |
| `evidence_refs[]` | Attached evidence |
| `created_at` | Timestamp |

No free-form strings between agents.

### 9.4 Independence

Real diversity comes from:

- Disjoint initial views
- Different evidence subsets
- Different models / prompts
- Independent retrieval
- Delayed cross-view exposure

**Never** give Agent B Agent A's conclusions before B finishes.

### 9.5 Auditor Gate

Gate on:

- Critical claim conflict
- Insufficient evidence
- High-impact classification
- Novel attack pattern
- Source disagreement
- Low evidence coverage
- Agent disagreement
- Suspicious tool behavior

Not just contradiction score.

---

## 10. Decision Engine

### 10.1 Multi-Dimensional Decision

Independent dimensions:

- phishing
- impersonation
- credential_theft
- payment_fraud
- malware
- social_manipulation

Plus:

- evidence_confidence
- evidence_freshness
- evidence_conflict

### 10.2 DecisionRecord

| Field | Description |
|---|---|
| `investigation_id` | Investigation |
| `resolved_claims[]` | Resolved claims |
| `unresolved_claims[]` | Unresolved claims |
| `contradictions[]` | Contradictions |
| `threat_classes[]` | Threat classifications |
| `evidence_coverage` | Coverage metrics |
| `policy_version` | Policy version |
| `scorer_version` | Scorer version |

### 10.3 Convergence

```
required claim scopes resolved
∧ no unresolved critical contradiction
∧ minimum evidence policy satisfied
```

Budget exhausted → insufficient evidence (not convergence).

---

## 11. Grounded Response

### 11.1 Claim-ID Anchored Prose

Every sentence maps to one or more claim IDs:

```
[C17] The domain matches a known phishing record.
[C24] The message requests a UPI payment.
```

### 11.2 Structural Grounding

Grounding checker verifies: `sentence → claim refs → evidence refs`

### 11.3 Fail-Closed Templates

- `VERIFIED_THREAT_TEMPLATE`
- `SUSPICIOUS_TEMPLATE`
- `INSUFFICIENT_EVIDENCE_TEMPLATE`
- `BENIGN_WITH_LIMITATIONS_TEMPLATE`
- `ANALYSIS_UNAVAILABLE_TEMPLATE`

---

## 12. Security Model

### 12.1 OWASP Agentic Top 10 Mapping

| Threat | Mitigation |
|---|---|
| ASI01 Agent Goal Hijack | Evidence/Instruction distinction, typed messages |
| ASI02 Tool Misuse | Agent identity, allowed_tools, capability invariant |
| ASI03 Identity & Privilege Abuse | Agent identity, scoped permissions |
| ASI04 Supply Chain | Pinned dependencies, sandboxed execution |
| ASI05 Unexpected Code Execution | Hardened artifact sandbox |
| ASI06 Memory & Context Poisoning | Immutable snapshots, append-only ledger |
| ASI07 Insecure Inter-Agent Communication | Typed AgentMessage |
| ASI08 Cascading Failures | Budget limits, timeouts, DLQ |
| ASI09 Human-Agent Trust Exploitation | Grounded response, fail-closed templates |
| ASI10 Rogue Agents | Agent identity, audit chain |

### 12.2 Capability Invariant

No component that holds attacker-controlled bytes has tools, network access, or write
capability. All untrusted input crosses a schema-validated boundary before any privileged
component can act on it.

---

## 13. Audit Chain

### 13.1 Scoped Sequence

```
payload_hash = SHA256(raw_body)

h_n = SHA256(
  "VOUCH-AUDIT-V1" ||
  tenant_id ||
  sequence_number ||
  event_type ||
  payload_hash ||
  h_(n-1)
)
```

### 13.2 External Anchoring

Periodically anchor chain head into a separate trust domain / WORM store. A database-only
hash chain is tamper-evident; an administrator who can rewrite the DB can also rewrite the
chain.
