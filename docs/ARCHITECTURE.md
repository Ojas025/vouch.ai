# Vouch: Autonomous Multi-Agent Threat & Fraud Intelligence Engine
## Production Architecture Specification (v2.0 — Hardened)

---

## 1. Executive Summary & Design Principles

**Vouch** is a production-grade, multi-agent threat and fraud intelligence engine designed to investigate user-forwarded suspicious messages (SMS, WhatsApp, Telegram). It moves beyond passive keyword filtering and ungrounded LLM chatbots by implementing **closed-loop forensic investigation, asymmetric multi-agent adjudication, and typed decision calibration**.

### Core Architecture Pillars

1. **Deterministic System of Record (PostgreSQL over Redis):** PostgreSQL is the durable source of truth for the entire submission lifecycle (`Submission → Evidence → Finding → Decision → Dossier`). Redis Stream serves strictly as an ephemeral, at-least-once message delivery broker.
2. **Inside-Out Execution (Core to Perimeter):** Domain logic, forensic tools, and the multi-agent engine are built as headless, testable Python modules. Messaging gateways (Telegram, WhatsApp) are thin, decoupled I/O adapters.
3. **Two-Stage Asynchronous Delivery:** Fast synchronous gateway ACK ($<15\text{ms}$ to Meta/Telegram webhooks, $<500\text{ms}$ user feedback) coupled with deferred asynchronous dossier delivery for deep investigation cache misses ($4–8\text{s}$).
4. **Prior Evidence Re-Validation on Cache Hits:** Cache hits do not blindly emit stale verdicts. They retrieve prior evidence dossiers and re-validate them against current extracted entities to prevent template collision false positives.
5. **Independent Gathering → Contradiction Auditor → Adjudication DAG:** Rather than conversational debate loops, agents gather evidence independently in parallel, submit strict typed structs to a shared Blackboard, pass through a Contradiction Auditor, and are arbitrated by Jev in $<50\text{ms}$.
6. **Zero-Free-Text Blackboard Protocol:** The multi-agent state space communicates strictly via typed, schema-validated Pydantic structures with scalar values and cryptographic source hashes. Free-form text is banned from the shared blackboard to physically eliminate indirect prompt injection (IPI).
7. **Two-Phase Uncertainty Calibration (Cold-Start Safe):** Acknowledges that Conformal Prediction requires an empirical calibration corpus ($\ge 500$ verified cases). Runs an explicit Phase-1 flat threshold gate, migrating to true Split Conformal Prediction once the HITL queue has produced sufficient ground truth.
8. **Versioned Graph Mutations with Rollback:** Every node and edge mutation in the Threat Graph carries an `investigation_id` and version tag. Faulty convictions or overturned verdicts can be rolled back atomically without network corruption.
9. **Idempotency Across Worker Redeliveries:** Outbound dispatchers are keyed on `(submission_id, dispatch_stage)` in PostgreSQL to guarantee zero duplicate messages sent to users during Celery/ARQ worker retries.

---

## 2. End-to-End Pipeline Architecture

```
[Telegram / WhatsApp / REST Client]
                 │
                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Gateway & Ingestion Layer                                                │
│ • Channel Adapters normalize input to UserSubmission                        │
│ • PostgreSQL Durable Store: Creates Submission record in 'RECEIVED' state   │
│ • Append-Only WORM Audit Log: SHA-256 Merkle Chained byte-level storage     │
│ • Fast ACK: HTTP 200 OK returned to webhook in <15ms                        │
│ • Task Enqueued: Pushes task to Redis Stream (Ephemeral Task Queue)         │
└─────────────────────────────────────────────────────────────────────────────┘
                 │
                 ▼ (Asynchronous Worker Pool — Celery / ARQ)
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. Normalization, Entity Extraction & Intent Routing                        │
│ • NFKC De-Obfuscator: Canonical recomposition, UTR #39 confusable mapping,  │
│   zero-width character stripping, intra-word collapse (P.a.y -> Pay)        │
│ • Deterministic Entity Extractor: Regex/pattern extraction for E.164 phones,│
│   UPI VPAs, domains, bank IFSCs, and monetary figures                       │
│ • Jev Intent Classifier: Sub-30ms typed categorical decision                │
│   Choice[scam_fraud, misinfo_claim, ai_gen_check, unclear]                  │
└─────────────────────────────────────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. Cache & Relational Graph Triage                                          │
│ • Tier 1: MinHash LSH Index (Word 3-grams, Jaccard >= 0.75)                 │
│ • Tier 2: ONNX Dense Semantic Centroids (all-MiniLM-L6-v2, Cosine >= 0.88) │
│ • Tier 3: Bipartite Threat Graph Proximity (Indexed Phone / UPI / Domain)   │
│   ├── [Cache / Graph Hit] ──► Prior Evidence Re-Validator                  │
│   │     ├── Validated ──────► Idempotent Fast Return (<1s)                  │
│   │     └── Invalidation ───► Fall through to Investigator Registry         │
│   └── [Cache Miss] ─────────► Fall through to Investigator Registry         │
└─────────────────────────────────────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 4. Jev Magistrate: Inquest & Planning Directives (src/vouch/jev.py)         │
│ • Single-Vector Fast-Path: Bypasses LLM planner in 0ms for basic text/SMS. │
│ • Multimodal Inquest: Decomposes composite forwards (OCR screenshots, APK  │
│   files, complex hybrid claims) into a typed Pydantic InvestigationPlan.    │
│ • Parallel Branch Commissioning: Directs Accuser & Defender to targeted     │
│   forensic branches (Domain, Telecom, Institutional Legitimacy, Payload).   │
└─────────────────────────────────────────────────────────────────────────────┘
                 │ (Investigative Branches Dispatched)
                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 5. Deep Investigation Engine: Asymmetric Gathering & Adjudication DAG       │
│                                                                             │
│   [Parallel Independent Gathering]                                          │
│   ├── Agent 1: Accuser (Fraud Intelligence)                                 │
│   │   Tools: Abuse.ch URLhaus (Phishing/APKs), ThreatFox, SearXNG Sifter    │
│   └── Agent 2: Defender (Institutional Verifier)                            │
│       Tools: phonenumbers + TRAI 160-Series Check, ICANN RDAP (Domain Age), │
│              RBI Regex + Razorpay IFSC Fallback, Official Bulletin Sifter   │
│                               │                                             │
│                               ▼                                             │
│   [Air-Gapped Extraction Worker]                                            │
│   Generative LLM (Zero Network, Zero Tools) -> Extracts Structured JSON     │
│                               │                                             │
│                               ▼                                             │
│   [Deterministic Code Gate]                                                 │
│   • Domain allowlist verification (Python Set lookup)                       │
│   • Verbatim Substring Grounding (Exact / Levenshtein >= 95 match)          │
│   • Citation Entailment Check (NLI Natural Language Inference pass)         │
│                               │                                             │
│                               ▼                                             │
│   [Strict Structured Blackboard]                                            │
│   Pydantic Structs Only (Zero Free-Text Narrative, Hash-Referenced Sources) │
│                               ▲                                             │
│                               │ (Inspects Clashes)                          │
│   [Agent 3: Contradiction Auditor]                                          │
│   Air-Gapped Pure Reasoner: Flags discrepancies between Accuser & Defender  │
│                               │                                             │
│                               ▼                                             │
│   [Jev Magistrate / Adjudicator]                                            │
│   Sub-50ms Sufficiency & Convergence Decision (Max 2 Rounds)                │
└─────────────────────────────────────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 6. Decision, Calibration & Output Layer                                     │
│ • Two-Phase Uncertainty Gate:                                               │
│   ├── Phase 1 (Bootstrap <500 cases): Static threshold band (0.35 - 0.70)   │
│   └── Phase 2 (Production >=500 cases): Split Conformal Prediction (ICP)    │
│ • Human-in-the-Loop (HITL) Return Path:                                     │
│   Queue -> Operator Signed Label -> DB State -> Dossier -> Graph -> Dispatch│
│ • Versioned Graph Mutation: Commits nodes/edges with investigation_id & TTL │
│ • Idempotent Dispatcher: Keyed on (submission_id, dispatch_stage)           │
│   Delivers Stage-2 Deferred Dossier to Telegram / WhatsApp / REST Client    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Deep Component Specifications

### 3.1 Gateway, Ingestion & System of Record (PostgreSQL)

*   **Durable State Machine:** PostgreSQL is the authoritative system of record. Every submission progresses through explicit state transitions:
    $$\text{RECEIVED} \longrightarrow \text{PREPROCESSED} \longrightarrow \text{CACHE\_CHECKED} \longrightarrow \text{INVESTIGATING} \longrightarrow \text{ADJUDICATED} \longrightarrow \text{DISPATCHED}$$
*   **Fast Ingestion SLA ($<15\text{ms}$):**
    1.  Assigns an immutable `UUIDv7` (time-ordered primary key).
    2.  Computes `SHA-256(raw_bytes)` and inserts into `submissions` table in PostgreSQL.
    3.  Pushes `{submission_id, raw_bytes, channel, sender_id}` to a Redis Stream (`XADD vouch:ingest * ...`).
    4.  Immediately returns `HTTP 200 OK` to the WhatsApp/Telegram webhook.
*   **Append-Only WORM Audit Store:** Raw bytes and metadata are saved in an append-only audit table with cryptographic hash chaining:
    $$\text{Hash}_N = \text{SHA-256}(\text{Payload}_N \parallel \text{Hash}_{N-1})$$
    Guarantees tamper-evident chain of custody for legal and regulatory compliance.

---

### 3.2 Preprocessing, NFKC Canonicalization & Entity Extraction

*   **NFKC Normalization (The Fix):** Replaces NFKD with **NFKC (Normalization Form Compatibility Composition)**.
    *   *Why:* NFKC decomposes compatibility characters and recomposes them into standard canonical forms, correctly folding homoglyph lookalikes, ligature splits, and full-width forms into standard ASCII.
*   **Adversarial De-Obfuscation Pipeline:**
    1.  *Invisible Character Filter:* Strips Unicode categories `Cf` (Format), `Cc` (Control), and explicit codepoints (`\u200B`–`\u200F`, `\uFEFF`, `\u00AD`).
    2.  *UTR #39 Confusable Mapping:* Maps visually identical Cyrillic, Greek, and Cherokee characters to base Latin ASCII (`Cyrillic а` $\to$ `Latin a`, `Greek ο` $\to$ `Latin o`).
    3.  *Intra-Word Collapse:* Normalizes punctuation and spaces bounded by single characters:
        `re.sub(r'(?<=\b\w)[.\-_ ](?=\w\b)', '', text)` (e.g., `p.a.y.m.e.n.t` $\to$ `payment`).
    4.  *Leetspeak Normalization:* Deterministic substitution of isolated symbols (`@` $\to$ `a`, `$` $\to$ `s`, `0` $\to$ `o`, `1` $\to$ `i`).
*   **Deterministic Entity Extractor (The Prerequisite for Cache/Graph):**
    *   Sits directly between Canonicalization and Cache Check.
    *   Extracts all identifiers via validated regex patterns:
        *   `extracted_phones`: Normalized to E.164 (`+919876543210`) using Google's `phonenumbers`.
        *   `extracted_vpas`: Regex validated against NPCI PSP handle formats (`user@bank`).
        *   `extracted_domains`: FQDNs extracted and punycode-decoded.
        *   `extracted_ifscs`: 11-character bank branch codes (`^[A-Z]{4}0[A-Z0-9]{6}$`).

---

### 3.3 Cache Triage & Prior Evidence Re-Validation

*   **Lexical Pass:** MinHash LSH (128 permutations, 16 bands $\times$ 8 rows) over masked word 3-grams ($J \ge 0.75$).
*   **Semantic Pass:** Local, quantized `all-MiniLM-L6-v2` ONNX embedding centroids ($\text{Cosine} \ge 0.88$).
*   **Graph Proximity Pass:** $O(1)$ relational indexed lookup on extracted phone, UPI VPA, and domain nodes.
*   **Prior Evidence Re-Validator (The Anti-Blind-Hit Rule):**
    *   A cache or graph hit does **not** directly output a final verdict.
    *   It retrieves the **prior evidence dossier** and runs a deterministic compatibility check:
        1.  Do the critical entities match (same phone/UPI handle)?
        2.  Is the prior evidence still within its TTL window ($<30\text{ days}$)?
        3.  Does the new message contain novel claims that contradict the prior evidence?
    *   *Validated Hit:* Emits fast provisional verdict in $<1\text{s}$.
    *   *Invalidated Hit:* Re-routed to the Investigator Registry for a fresh investigation.

---

### 3.4 The Unified Jev Magistrate Lifecycle & Adjudication DAG

Instead of disjointed router agents or conversational debate ping-pong, Vouch unifies investigation planning and ruling under the **Jev Magistrate (`src/vouch/jev.py`)**, mirroring the civil law *Investigating Magistrate* pattern:

1. **Phase 1: Jev Inquest (Directive & Planning):** Analyzes the incoming message and multimodal facets, formulating an initial hypothesis and issuing typed directives (`InvestigationPlan`).
2. **Phase 2: Independent Gathering & Strict Blackboard:** Dispatched branches (Accuser & Defender) execute non-blocking OSINT and write immutable, typed claims to the Blackboard.
3. **Phase 3: Jev Adjudication (Ruling & Sufficiency):** Jev inspects the Blackboard against its own initial inquest directives, checks the Contradiction Auditor for clashes, and delivers the calibrated ruling.

```
[ Inbound Forward (Text + Multimodal Attachments) ]
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ 1. Jev Inquest: Directive & Plan Synthesis (Phase 1)        │
│ • Simple SMS? ──► Deterministic Fast-Path (<0.5ms bypass)   │
│ • Multimodal? ──► Emits typed Pydantic InvestigationPlan    │
│   (Directs Accuser & Defender to targeted forensic branches)│
└────────────────────────┬────────────────────────────────────┘
                         │
        ┌────────────────┴────────────────┐
        ▼                                 ▼
┌────────────────────────┐      ┌────────────────────────┐
│ Branch A: Technical    │      │ Branch B: Contextual   │
│ • RDAP Domain Age      │      │ • Official Bulletins   │
│ • Abuse.ch URLhaus     │      │ • TRAI 160-Series Check│
│ • Rogue APK Analysis   │      │ • RBI IFSC Routing     │
└───────────┬────────────┘      └───────────┬────────────┘
            │                               │
            └────────────────┬──────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. Air-Gapped Extraction & Deterministic Code Gate          │
│ • Extraction Worker (Zero Tools) -> Structured JSON Claims  │
│ • Verbatim Substring Grounding & Citation Entailment Check  │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. The Strict Structured Blackboard                         │
│ • Stores Pydantic Structs Only (Zero Free-Text Narrative)   │
│ • Agent 3 (Contradiction Auditor) detects semantic clashes  │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. Jev Adjudication & Convergence (Phase 2)                 │
│ • Evaluates unrefuted claims against Inquest Directives     │
│ • Round < 2 & Clashes -> Targeted Re-Gathering Directive    │
│ • Round == 2 or Convergence -> Synthesize Decision         │
└─────────────────────────────────────────────────────────────┘
```

#### The Investigation Plan Schema
To prevent prompt-injected plan hijacking, Jev's inquest plan is constrained to verified branch types:

```python
class InvestigationBranch(BaseModel):
    branch_type: Literal[
        "TELECOM_FORENSICS",      # 160s, VoIP, Carrier
        "DOMAIN_INFRASTRUCTURE",  # RDAP, URLhaus, MX records
        "INSTITUTIONAL_CLAIM",    # RBI/IFSC, official bank bulletins
        "PAYLOAD_ANALYSIS"        # APK MIME, shortlink redirects
    ]
    target_entity: str            # e.g., "https://sbi-update.xyz" or "+919876543210"
    priority: int = 1

class InvestigationPlan(BaseModel):
    is_composite: bool
    rationale: str
    active_branches: list[InvestigationBranch]
    budget_timeout_ms: int = 3500
```

#### The Zero-Free-Text Blackboard Protocol
To physically prevent prompt-injection leaks between agents, free-text strings are strictly forbidden from the Blackboard. Every claim is an immutable, validated struct:

```python
class BlackboardEntry(BaseModel):
    entry_id: UUID
    agent_role: Literal["accuser", "defender"]
    claim_type: Literal[
        "carrier_line_type",
        "domain_registration_age",
        "payment_handle_format",
        "bank_branch_validated",       # Corrected from oversold "UPI Merchant Verified"
        "official_discom_bulletin_status",
        "public_complaint_cluster"
    ]
    subject: str = Field(..., pattern=r"^(\+[1-9]\d{1,14}|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}|[a-zA-Z0-9._-]+@[a-zA-Z0-9]+)$")
    predicate: Literal[
        "is_personal_mobile_sim",
        "is_voip_burner",
        "registered_days_ago",
        "branch_code_exists",          # Razorpay IFSC narrow predicate
        "matches_known_discom_advisory",
        "zero_complaints_found_in_corpus"
    ]
    value: Union[int, float, bool, Literal["discom_advisory_match", "clean_record"]]
    evidence_source_id: str
    source_citation_hash: str = Field(..., min_length=64, max_length=64) # SHA-256
```

#### Deterministic Code Gate: Substring Grounding + Citation Entailment Check
*   **Step 1: Substring Grounding:** Verifies that the cited quote exists verbatim (or Levenshtein $\ge 95$) in the raw fetched document text.
*   **Step 2: Citation Entailment Check (The Fix):** Substring match only proves the text *exists*, not that it *supports the claim*. A lightweight Natural Language Inference (NLI) model (or typed Jev entailment call) classifies the relationship between `verbatim_quote` and `extracted_claim`:
    *   `ENTAILS` $\to$ Approved for Blackboard write.
    *   `CONTRADICTS` or `NEUTRAL` $\to$ Rejected at the gate.

---

### 3.5 Real-World Data Access & Fallback Matrix (Updated 2026 Standards)

| Forensic Dimension | Dream / Enterprise Source | Real-World Accessibility | Vouch Production Workaround (2026 Standards) |
| :--- | :--- | :--- | :--- |
| **Phone Line Type & Regulatory Class** | Telecom HLR / SS7 Lookup | Gated / Paid telco APIs | **Google `phonenumbers` + TRAI 160-Series Gate:** Local Python library classifies mobile, fixed-line, and VoIP ($0.00). Immediately flags banking/KYC alerts originating from standard 10-digit mobile SIMs (`+91 9...`) as post-DLT smishing bypasses. |
| **Bank Branch & Routing** | Reserve Bank of India DB | Gated internal databases | **RBI Regex + Razorpay Public IFSC API:** Deterministic local regex (`^[A-Z]{4}0[A-Z0-9]{6}$`) followed by Razorpay IFSC REST API for branch location and RTGS status with cached offline fallback ($0.00). |
| **UPI Merchant Validation** | NPCI "Verify VPA 360" API (`isMerchant`, `mcc`) | Gated behind licensed Payment Aggregator (Juspay/Setu) KYC | **Two-Tier Verification:** Regex validates against NPCI's 80+ official PSP handles; SearXNG checks handle reputation; Fallback Downgrade Policy applied. |
| **SMS Sender & Channel Authenticity** | TRAI DLT Header Registry (e.g. `AD-HDFCBK-T`) | Gated behind Indian telecom enterprise portals | **Post-DLT Delivery Gate:** Legitimate financial alerts require 160-series numbers or registered 6-alpha DLT headers with typed tags (`{#url#}`, `{#cbn#}`). Any unwhitelisted URL, generic shortener (bit.ly), or rogue `.apk` payload forwarded via WhatsApp/Telegram is flagged as DLT-evasion smishing. |
| **Domain Lifecycle & Age** | ICANN Central Registry (Post-WHOIS Sunset) | 100% Open & Standardized | **ICANN RDAP REST Client & `dnspython`:** WHOIS is sunset as of Jan 2025; under ICANN's Registration Data Policy (in effect Aug 2025/May 2026), registrant PII is redacted. Vouch queries standardized RDAP (`https://rdap.org/domain/{domain}`) to compute exact **Domain Age (NRDs <14 days)**, registrar IANA ID, lifecycle status (`clientHold`), and checks MX record existence ($0.00). |
| **Malware & Phishing Threat DB** | Proprietary threat feeds | Expensive B2B contracts | **Abuse.ch URLhaus & ThreatFox:** URLhaus provides real-time JSON lookups for active phishing URLs and rogue `.apk` download drops; ThreatFox provides auxiliary IOC correlation ($0.00). |
| **Public Precedents & News** | State Police FIR / CCTNS | Gated behind citizen/police auth | **Self-Hosted SearXNG:** Queries public cybercell circulars, consumer complaint forums, and official discom press releases ($0.00). |

---

### 3.6 Two-Phase Uncertainty Calibration & Closed-Loop HITL

#### Two-Phase Calibration Roadmap
1.  **Phase 1 (Bootstrap & Cold Start — $<500$ Cases):**
    *   Operates on static heuristic score bands:
        *   $p \ge 0.85 \to$ High Confidence Conviction.
        *   $p \le 0.20 \to$ High Confidence Clean.
        *   $0.35 \le p \le 0.70 \to$ Route to HITL Queue.
    *   Every investigation logs `{submission_id, jev_scores, extracted_features, human_label}` to PostgreSQL.
2.  **Phase 2 (Production Scale — $\ge 500$ Verified Cases):**
    *   Migrates to **Inductive Conformal Prediction (ICP)** using Non-Conformity Scoring over the calibration database.
    *   Produces mathematically guaranteed coverage sets for error tolerance $\alpha = 0.02$.
    *   Weekly automated recalibration job runs to detect and adjust for concept drift.

#### Closed-Loop HITL Return Path
```
[HITL Review Queue (Web UI / Slack)]
                 │
                 ▼
[Operator Review & Signed Cryptographic Label]
                 │
                 ▼
[HITL Return Pipeline]
├── 1. Updates PostgreSQL: state = 'ADJUDICATED', sets human_verified = True
├── 2. Synthesizes Final Investigation Dossier
├── 3. Executes Versioned Graph Mutation
└── 4. Calls Idempotent Channel Dispatcher to notify user
```

---

### 3.7 Versioned Graph Mutations & Idempotent Dispatch

#### Versioned Graph Mutations with Atomic Rollback
*   Every node and edge inserted into the Threat Graph carries:
    *   `source_investigation_id`: UUID of the originating submission.
    *   `mutation_version`: Monotonically increasing schema version.
    *   `status`: `PROVISIONAL` (auto-convicted) or `CONFIRMED` (HITL verified).
    *   `expires_at`: 30-day rolling TTL.
*   **Atomic Rollback Support:** If an investigation is overturned by a human reviewer or customer appeal:
    ```sql
    DELETE FROM threat_edges WHERE source_investigation_id = :investigation_id;
    DELETE FROM threat_nodes WHERE source_investigation_id = :investigation_id AND degree = 0;
    ```
    Guarantees that a single corrupted investigation cannot permanently poison the global detection graph.

#### Idempotent Channel Dispatcher
*   To prevent duplicate messages during Celery/ARQ worker crashes and retries:
    *   The dispatcher acquires an atomic advisory lock on `(submission_id, dispatch_stage)`.
    *   Checks PostgreSQL `dispatch_logs`: if `status == 'DELIVERED'`, the task exits immediately as a no-op.
    *   Once outbound API returns success, commits `status = 'DELIVERED'` with the external platform message ID.

---

## 4. Formal Data Contracts & Schemas

```python
from pydantic import BaseModel, Field, HttpUrl
from typing import Literal, Union, Optional
from uuid import UUID
from datetime import datetime

# 1. Gateway Ingestion Schema
class UserSubmission(BaseModel):
    submission_id: UUID
    channel: Literal["telegram", "whatsapp", "rest_api"]
    user_identifier: str
    raw_payload: str
    raw_sha256: str = Field(..., min_length=64, max_length=64)
    received_at: datetime

# 2. De-Obfuscated Canonical Text Envelope
class CanonicalEnvelope(BaseModel):
    submission_id: UUID
    raw_sha256: str
    canonical_text: str
    extracted_urls: list[str] = []
    extracted_phones: list[str] = []
    extracted_vpas: list[str] = []
    extracted_ifscs: list[str] = []
    extracted_amounts: list[str] = []
    has_urgency_trigger: bool

# 3. Blackboard Structured Entry (Zero Free-Text)
class BlackboardEntry(BaseModel):
    entry_id: UUID
    agent_role: Literal["accuser", "defender"]
    claim_type: Literal[
        "carrier_line_type",
        "domain_registration_age",
        "payment_handle_format",
        "bank_branch_validated",
        "official_discom_bulletin_status",
        "public_complaint_cluster"
    ]
    subject: str = Field(..., pattern=r"^(\+[1-9]\d{1,14}|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}|[a-zA-Z0-9._-]+@[a-zA-Z0-9]+)$")
    predicate: Literal[
        "is_personal_mobile_sim",
        "is_voip_burner",
        "registered_days_ago",
        "branch_code_exists",
        "matches_known_discom_advisory",
        "zero_complaints_found_in_corpus"
    ]
    value: Union[int, float, bool, Literal["discom_advisory_match", "clean_record"]]
    evidence_source_id: str
    source_citation_hash: str = Field(..., min_length=64, max_length=64)

# 4. Verified Citation Contract
class VerifiedCitation(BaseModel):
    source_url: str
    source_title: str
    verbatim_quote: str
    sha256_digest: str
    substring_match_passed: bool = True
    nli_entailment_passed: bool = True

# 5. Polymorphic Final Investigation Dossier
class ScamInvestigationDossier(BaseModel):
    submission_id: UUID
    verdict: Literal["CONFIRMED_SCAM", "PROVISIONALLY_CLEAN", "EXONERATE_LEGITIMATE", "NEEDS_HUMAN_REVIEW"]
    calibrated_confidence: float = Field(..., ge=0.0, le=1.0)
    scam_category: Literal[
        "utility_cutoff_fraud",
        "part_time_job_task",
        "digital_arrest_extortion",
        "banking_kyc_phishing",
        "lottery_advance_fee",
        "unclassified"
    ]
    threat_severity: int = Field(..., ge=1, le=5)
    debunked_claims: list[str]
    citations: list[VerifiedCitation]
    action_advice: str
    audit_chain_hash: str
    total_latency_ms: int
```

---

## 5. Summary of Architecture Corrections (v1.0 → v2.0)

| Fault Identified | Architectural Root Cause | v2.0 Production Fix |
| :--- | :--- | :--- |
| **NFKD Normalization** | Accidental decomposition typo; failed to fold compatibility glyphs | Replaced with **NFKC** canonical recomposition. |
| **Graph Query Missing Input** | Graph fast-path was called before entities were extracted | Inserted **Deterministic Entity Extractor** directly after canonicalization. |
| **Redis Stream as Source of Truth** | Ephemeral queues drop state on worker crashes | Instate **PostgreSQL** as authoritative state machine; Redis is ephemeral queue only. |
| **Blind Cache Hits** | Cache hits emitted final verdicts without checking current context | Added **Prior Evidence Re-Validator** to re-verify cached claims against current entities. |
| **Conversational Debate Overhead** | Ping-pong debate loops introduce non-deterministic latency | Streamlined to **Independent Gathering $\to$ Contradiction Auditor $\to$ Adjudication DAG**. |
| **Free-Text Blackboard Injection** | Strings on blackboard created an internal prompt injection vector | Enforced **Zero-Free-Text Pydantic Struct Protocol** with typed scalar values. |
| **Oversold Razorpay IFSC Scope** | Razorpay IFSC does not verify UPI merchant accounts | Narrowed scope to **Bank Branch Existence & Routing Validation**. |
| **Premature Conformal Prediction** | Presented conformal prediction without an empirical calibration set | Defined **Two-Phase Roadmap:** Static thresholding ($<500$) $\to$ ICP ($\ge 500$). |
| **Broken HITL Return Path** | Queue had no defined mechanism to resume the pipeline | Formalized **Closed HITL Return Loop:** Human Review $\to$ DB State $\to$ Dossier $\to$ Graph $\to$ Dispatch. |
| **Poisoned Threat Graphs** | Inverted or flawed convictions permanently corrupt the graph | Added **Versioned Graph Mutations** with atomic rollback by `investigation_id`. |
| **Citation Hallucination (Entailment)** | Substring grounding only checks text presence, not logical support | Added **Citation Entailment (NLI) Verification Gate** in code gate. |
| **Legacy WHOIS Protocol** | WHOIS sunset by ICANN in Jan 2025; registrant PII redacted by default | Upgraded to **ICANN RDAP REST Client + `dnspython`** focusing on NRD (<14d) and MX records. |
| **Post-DLT Smishing Evasion** | Scammers bypass telecom DLT filtering via 10-digit SIMs & WhatsApp APKs | Enforced **TRAI 160-Series / 10-Digit Anomaly Gate** & WhatsApp APK payload detection. |
| **Threat Intelligence Scope** | ThreatFox is malware-centric; doesn't prioritize real-time smishing landing pages | Standardized on **Abuse.ch URLhaus** as primary phishing/malware URL and drive-by APK feed. |
| **Fragile External Banking Dependency** | Single third-party API point-of-failure for every banking check | Added deterministic offline **RBI Regex Gate** (`^[A-Z]{4}0[A-Z0-9]{6}$`) with cached Razorpay fallback. |
| **Rigid 1-of-N Router Blindness** | Simple classifier forced composite multimodal forwards into single silos | Unified under **Jev Magistrate (`jev.py`)**: Phase 1 Inquest (with 0ms fast-path & multimodal `InvestigationPlan`) $\to$ Phase 2 Adjudication. |
