# Vouch — Autonomous Threat & Fraud Intelligence Engine

> High-throughput, sub-millisecond near-duplicate triage, air-gapped asymmetric multi-agent adjudication DAG, and mathematically calibrated fraud decisions for user-forwarded suspicious messages (SMS, WhatsApp, Telegram).

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Architecture Spec](https://img.shields.io/badge/spec-ARCHITECTURE.md-green.svg)](docs/ARCHITECTURE.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 1. Executive Overview

Consumer communication channels (SMS, WhatsApp, Telegram) face an unprecedented surge in AI-generated smishing and social engineering. Most consumer tools rely on naive LLM chatbot wrappers that suffer from **OWASP LLM01:2025 (Indirect Prompt Injection)**, hallucinate false positives, and introduce multi-second latencies.

**Vouch** solves this by engineering a decoupled, 4-tier security pipeline:
1. **Sub-Millisecond Triage Engine (<0.5ms):** Resolves ~80% of duplicate broadcast campaigns on CPU using Unicode NFKC de-obfuscation, slot masking, and a 128-permutation **MinHash LSH** cache at $0.00 LLM cost.
2. **Stateless Async OSINT Micro-Registry (<400ms):** Concurrently gathers verifiable ground truth across telecom, domain, and threat databases via standardized, non-blocking fetchers.
3. **Air-Gapped Adjudication DAG:** Shields the system from indirect prompt injection via a tool-less schema extractor and a **Strict Structured Pydantic Blackboard** where an **Accuser Agent** (fraud intel) and a **Defender Agent** (institutional legitimacy) debate before the **Jev Magistrate**.
4. **Split Conformal Prediction Gate:** Replaces arbitrary confidence percentages with empirical statistical guarantees (95% marginal coverage), automatically routing multi-label uncertainties to a Human-in-the-Loop (HITL) queue.

---

## 2. Updated 2026 Technical & Regulatory Standards

Vouch incorporates the latest protocol and regulatory developments:

| Forensic Vector | Legacy / Outdated Approach | **Vouch 2026 Production Standard** |
| :--- | :--- | :--- |
| **Domain Lifecycle & Age** | Plaintext WHOIS scraping | **ICANN RDAP REST Protocol + `dnspython`:** Compliant with ICANN's post-WHOIS sunset and Registration Data Policy (August 2025 / May 2026). Registrant PII is redacted; Vouch flags **Newly Registered Domains (NRDs <14 days)**, registrar IANA ID, lifecycle status (`clientHold`), and missing `MX` mail records. |
| **Telecom & Delivery Channel** | Generic E.164 parsing | **TRAI 160-Series & Post-DLT Bypass Gate:** Incorporates India's TRAI mandates (DLT URL whitelisting & typed tags `{#url#}`). Immediately flags banking/KYC alerts originating from standard 10-digit mobile SIMs (`+91 9...`) or unwhitelisted rogue `.apk` links forwarded on WhatsApp/Telegram. |
| **Threat Intelligence** | Generic C2 IOC lookups | **Abuse.ch URLhaus API:** Real-time specialized threat feed targeting active phishing landing pages, credential harvesters, and drive-by malware payloads. |
| **Banking & Payment Routing** | Fragile 3rd-party API reliance | **RBI 11-Char Regex Gate (`^[A-Z]{4}0[A-Z0-9]{6}$`) + NPCI Handle Validation:** Runs local offline validation first, with async Razorpay IFSC lookups for branch location and RTGS status. |
| **Agent Communication** | Free-form natural language debate | **Zero-Free-Text Pydantic Blackboard:** Prevents "persuasive inconsistency" and prompt injection by forcing all agent claims into typed scalar structs. |
| **Investigation Orchestration** | Rigid 1-of-N router / unconstrained LLM planners | **Unified Jev Magistrate (`src/vouch/jev.py`):** Acts as the civil-law Investigating Magistrate. Phase 1 Inquest decomposes composite multimodal forwards into typed `InvestigationPlan` directives (with 0ms fast-path bypass on simple SMS); Phase 2 Adjudication evaluates sufficiency and delivers the calibrated ruling. |

---

## 3. Architecture Blueprint

```
[ Raw Forwarded Message ]
           │
           ▼ (Tier 0: <1ms, CPU-only)
┌────────────────────────────────────────────────────────┐
│ 1. De-Obfuscator & Entity Extractor (normalizer.py)    │
│ • NFKC Normalization + Zero-Width Character Stripping  │
│ • Confusable Skeleton Folding (Cyrillic -> Latin)      │
│ • Deterministic Regex Extraction (Phones, URLs, IFSC)  │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼ (Tier 0: <0.5ms, In-Memory)
┌────────────────────────────────────────────────────────┐
│ 2. Tri-Pass Fast Cache & LSH (fingerprint.py)          │
│ • Variable Slot Masking (<PHONE>, <URL>, <AMOUNT>)     │
│ • 128-Permutation MinHash LSH Index                    │
│ • Cache Hit? ──► [ Instant Provisional Verdict (<500ms)]│
└──────────────────────────┬─────────────────────────────┘
                           │ Cache Miss
                           ▼ (Tier 1: <400ms, Concurrent Async HTTP)
┌────────────────────────────────────────────────────────┐
│ 3. Stateless OSINT Micro-Registry (tools/osint.py)     │
│ • asyncio.gather() across RDAP, URLhaus, & libphonenum  │
│ • Validates clean, typed OSINTEnrichment struct        │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼ (Air-Gapped & Grounded)
┌────────────────────────────────────────────────────────┐
│ 4. Air-Gapped Extractor & Code Gate (airgap.py)        │
│ • Tool-less LLM extracts semantic claims into JSON     │
│ • Python Code Gate: Verbatim quote substring check     │
│   and official institutional domain allowlists         │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼ (Multi-Agent Adjudication DAG)
┌────────────────────────────────────────────────────────┐
│ 5. Asymmetric Tool-MAD Blackboard (agents/)            │
│ • Strict Pydantic BlackboardState (Zero Free-Text)     │
│ • Accuser Agent (Fraud Intel) vs Defender Agent (Org)  │
│ • Contradiction Auditor detects semantic clashes       │
│ • Jev Magistrate: Sufficiency scoring & 2-round cap    │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼ (Statistical Calibration)
┌────────────────────────────────────────────────────────┐
│ 6. Split Conformal Prediction Gate (conformal.py)      │
│ • Evaluates non-conformity against threshold q_hat     │
│ • Singleton {SCAM} or {BENIGN} -> Synthesize Dossier   │
│ • Multi-Label {SCAM, BENIGN}   -> Route to HITL Queue  │
└────────────────────────────────────────────────────────┘
```

For the exhaustive specification, state transition tables, and fallback matrices, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## 4. Codebase Organization

```text
src/vouch/
├── config.py              # System settings, timeouts, and conformal alpha (0.05)
├── schemas.py             # Strongly typed Pydantic models (Blackboard, OSINT, Verdict)
├── normalizer.py          # Phase 1: NFKC, ZWSP strip, confusable skeleton folding
├── fingerprint.py         # Phase 1: Slot masking & 128-perm MinHash LSH index
├── tools/
│   └── osint.py           # Phase 2: Async ICANN RDAP, Abuse.ch URLhaus, phonenumbers, IFSC
├── airgap.py              # Phase 2: Tool-less schema extraction worker (OWASP immune)
├── code_gate.py           # Phase 2: Deterministic substring grounding & allowlists
├── blackboard.py          # Phase 3: Typed Pydantic blackboard state machine
├── agents/                # Phase 3: Asymmetric Adjudication DAG
│   ├── accuser.py         # Fraud Intel agent
│   ├── defender.py        # Institutional legitimacy agent
│   └── auditor.py         # Contradiction auditor
├── jev.py                 # Phase 3: Jev Magistrate (Phase 1 Inquest Planner + Phase 2 Adjudicator)
├── conformal.py           # Phase 4: Split conformal prediction gate
├── server.py              # Phase 4: FastAPI async intake endpoints
└── bot.py                 # Phase 4: Telegram bot adapter
```

---

## 5. Development Setup

### Prerequisites
* Python 3.12+
* [uv](https://github.com/astral-sh/uv) or standard Python `venv`

### Installation
```bash
git clone https://github.com/ojas/vouch.git
cd vouch
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Running Test Suite
```bash
pytest tests/ -v
```
