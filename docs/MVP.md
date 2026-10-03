# Vouch — MVP Build Plan

> **Fast and lean, not stripped.** Every major component stays: API server, queue, database,
> multi-agent DAG, dynamic blackboard, Jev's three roles, sandbox branch, all modalities. What
> changes is *depth and sequencing* — not whether a thing exists.
>
> **This is not a scam classifier competing on F1.** Scam detection is one routing target among
> several. Vouch is a forensic investigation service for forwarded artifacts: message, URL, APK,
> screenshot, PDF. The question is whether it produces **evidence a human can audit**, not whether
> it beats a regex.
>
> **v3.3 (2026-10-03):** thread intake in scope (`Submission.kind ∈ {single_artifact, thread}`);
> collectors moved to a **shared deterministic fact layer** (code, no LLM, TTL-cached `Fact`
> records); agents are **judgment specialists** in two adversarial teams plus gated team-neutral
> components (SMD/CSD detectors, Advisor, advice tail). See ARCHITECTURE.md §3.6 and §6.10.

---

## 0. The thesis, stated honestly

> **A forwarded artifact of unknown type can be investigated across modalities, with asymmetric
> agents gathering decorrelated evidence into a dynamic blackboard, producing a verdict whose
> reasoning is inspectable — and which abstains when the evidence does not support a call.**

### What is already taken — say so first

| Prior art | Status | Implication |
|---|---|---|
| **Meta WhatsApp Scam Alert** | Shipped, beta, 12 Aug 2026. On-device, **non-contacts only**, no forwarding path, no OSINT | Never claim "WhatsApp doesn't do this" |
| **Digiscam** | Live beta. Forward text/screenshot → 0–100, 10+ engines, still reads WHOIS | Closest consumer product; name it first |
| **JaanchKaro** | India, waitlist, ₹99/mo. OCR+ASR, cited Truth Score, human reviewer for novel claims | Same users, same languages |
| **ScamShield** | Open source. Telegram bot, categorical confidence, abstention, coverage metrics. **Rejects non-text modalities** | Closest open-source; differentiator is exactly the modalities it declines |
| **SAGE / AgentDroid / AuditAgent** | Academic multi-agent fraud + Android multi-modal frameworks (2025–26) | Multi-agent fraud detection is published, not novel |
| **Link checkers** (~40) | Reputation + heuristics | URL reputation is commoditised |

### Where Vouch is genuinely unoccupied

1. **Static file forensics as a consumer service.** Nobody runs static analysis on a forwarded APK.
   Manifest triage is **80 ms** [measured] and holds every signal that matters for a fake-KYC app.
2. **Documents analysed as documents,** not OCR'd. Structure, macro presence, provenance.
3. **Retractable, provenance-carrying verdicts** as a user-facing feature.
4. **First-class abstention** — "conflicting evidence, escalating to a human" as a product output.

**The strongest card is #1.** It is cheap, it is measurable, and it is empty in the market.

---

## 1. Pipeline position

```
 ┌─────────────────────────────────────────────────────┐
 │ GATEWAY (exists)   FastAPI · Telegram + WhatsApp     │
 │   verify → persist → XADD → ACK                     │
 └──────────────────────────┬──────────────────────────┘
                            ▼  Redis Stream
 ┌─────────────────────────────────────────────────────┐
 │ WORKER (exists)      consumer + XAUTOCLAIM          │
 └──────────────────────────┬──────────────────────────┘
                            ▼
 ┌─────────────────────────────────────────────────────┐
 │ FACT LAYER (1)   NFKC · UTR#39 · zero-width · LSH   │
 │   code, no LLM → typed Facts, TTL-cached            │
 └──────────────────────────┬──────────────────────────┘
                            ▼
 ┌─────────────────────────────────────────────────────┐
 │ JEV INQUEST                    [Jev role #1]         │
 │   fast-path OR InvestigationPlan + branch selection │
 └──────────────────────────┬──────────────────────────┘
                            ▼
 ┌─────────────────────────────────────────────────────┐
 │ FACT LAYER (2) — MODALITY BRANCH (dispatch on plan) │
 │   url+Browser │ apk+Androguard │ doc+oletools       │
 │   feeds · RDAP · IFSC · allowlists (shared, no LLM) │
 └──────────────────────────┬──────────────────────────┘
                            ▼
 ┌─────────────────────────────────────────────────────┐
 │ SMD / CSD DETECTORS   every submission ‖ thread     │
 │   CSD: rolling summary, adaptive cadence            │
 │   threat level = OR across paths (ARCH §3.6)        │
 └──────────────────────────┬──────────────────────────┘
                            ▼
 ┌─────────────────────────────────────────────────────┐
 │ SPECIALIST GATHERING  Accuser team ‖ Defender team  │
 │   disjoint judgment grants → typed claims only      │
 └──────────────────────────┬──────────────────────────┘
                            ▼
 ┌─────────────────────────────────────────────────────┐
 │ AIR-GAPPED EXTRACTION + CODE GATE                   │
 │   LLM: zero tools, zero network → unverified only   │
 └──────────────────────────┬──────────────────────────┘
                            ▼
 ┌─────────────────────────────────────────────────────┐
 │ DYNAMIC BLACKBOARD + AUDITOR          [Agent 3]      │
 │   claims · facts · contradictions · attempts[]      │
 │   standing_instructions[] · round tracking          │
 └──────────────────────────┬──────────────────────────┘
                            ▼
 ┌─────────────────────────────────────────────────────┐
 │ ADVISOR  [round boundary · team-neutral]             │
 │   reads attempts[] → append-only instruction deltas  │
 │   no tools · cannot alter grants or budget          │
 └──────────────────────────┬──────────────────────────┘
                            ▼
 ┌─────────────────────────────────────────────────────┐
 │ JEV ADJUDICATION                    [Jev role #2]    │
 │   sufficiency · convergence · re-gather [role #3]   │
 └──────────────────────────┬──────────────────────────┘
                            ▼
 ┌─────────────────────────────────────────────────────┐
 │ DOSSIER → DISPATCH  verdict · citations · abstain   │
 │   Advice Writer (no tools) → Narrative Verifier    │
 │   ungrounded prose → deterministic template        │
 └─────────────────────────────────────────────────────┘
```

Gateway, queue, and database are **already built** (4 commits). The MVP extends them; it does not
replace them.

---

## 2. Components

Each: what it does → inputs → outputs → side effects → pipeline position.

---

### C0 · Gateway *(exists — extend)*

**Does:** Accepts forwarded artifacts from Telegram, WhatsApp, and REST. Verifies, persists, enqueues,
ACKs.

- **Inputs:** provider webhook payload
- **Outputs:** `Submission` row + `XADD vouch:ingest {submission_id, channel, sender_id, payload}`
- **Side effects:** Postgres write, Redis XADD, outbound API reply
- **Pipeline position:** stage 0

- [x] Telegram + WhatsApp POST handlers, HMAC verification, Redis enqueue, logging
- [ ] **Fix D1:** populate `Submission(payload=…, sha256_digest=…)` — currently `NotNullViolation`
- [ ] **Fix D2:** `.hexdigest()` not `.digest()`; the value is computed and discarded today
- [ ] **Fix D3:** `GET /webhook/{channel}` echoing `hub.challenge` — required for Meta onboarding
- [ ] **Fix D4:** include `submission_id` in the XADD payload (workers can't join to the row today)
- [ ] **Fix D5:** `hmac.compare_digest` for the Telegram token
- [ ] Accept `attachment` payloads → `AttachmentMetadata` (all five `kind` values)
- [ ] **v3.3:** Accept `Submission.kind = thread` — ordered message list with per-message senders
      (ARCH §3.6). Single artifact remains the fallback path, not the permanent ceiling.
- [ ] Idempotency: `UNIQUE (channel, external_message_id)` → `ON CONFLICT DO NOTHING`

---

### C1 · Canonicalizer

**Does:** Defeats obfuscation and extracts the entities every later stage keys on.

- **Inputs:** `text: str`, attachment text
- **Outputs:** `Canonical { canonical_text, entities { phones[], vpas[], domains[], ifscs[], amounts[] }, normalization_ops[] }`
- **Side effects:** none — pure
- **Pipeline position:** stage 1

- [ ] NFKC compatibility folding — folds `Ｙｏｕｒ`→`Your`, `ﬁance`→`fiance` [verified]
- [ ] **UTR #39 confusable folding, separately** — NFKC does **not** fold Cyrillic `а` U+0430 or Greek `ο` U+03BF [verified]
- [ ] Strip `Cf`/`Cc` + zero-width codepoints
- [ ] Intra-word collapse covering **prefix/suffix** (`kyc.updates.xyz`, `verify-now.account`) *and* per-character (`p.a.y.m.e.n.t`)
- [ ] Entity extraction **before** masking; store masked key + entity sidecar
- [ ] Record which ops fired, so a verdict can explain *why* two messages matched

> Justified by arXiv:2608.12889 — classical text classifiers degrade to a Robustness Degradation Ratio
> of **0.988** under exactly this obfuscation, while clean-text performance does not predict
> adversarial robustness. The normalizer is not cosmetic; it is what makes any text classifier survive.

---

### C2 · Jev Inquest *(planner — Jev role #1)*

**Does:** Commits to an investigation hypothesis **before** gathering evidence, then dispatches
modality branches.

- **Inputs:** `Canonical`, `Attachment[]`
- **Outputs:** `InvestigationPlan { active_branches[], modality, max_branches, budget_timeout_ms }` — or a deterministic fast-path
- **Side effects:** none — planning only
- **Pipeline position:** stage 2

- [ ] Fast-path for simple text → single branch, no LLM call
- [ ] Composite/multimodal → plan naming 2+ branches
- [ ] `branch_type` closed `Literal`: `TELECOM_FORENSICS | DOMAIN_INFRASTRUCTURE | INSTITUTIONAL_CLAIM | PAYLOAD_ANALYSIS`
- [ ] **`target_entity` regex-anchored, no free-text `rationale`** — Pillar 6 violation otherwise
- [ ] Sets `budget_timeout_ms` — **budget authority** (role #3)
- [ ] Selects from branches; never invents them

> **Why plan before evidence:** a plan made after gathering is a summary. Committing while ignorant,
> then testing against evidence that could not be cherry-picked, is the entire value.

---

### C3 · Modality Branches

**Does:** Turns an artifact into typed evidence, using the cheapest tool that yields real signal.

- **Inputs:** artifact + `InvestigationPlan`
- **Outputs:** `list[EvidenceClaim]` with `provenance`
- **Side effects:** local compute; Playwright Chromium for URLs; outbound HTTP for feeds
- **Pipeline position:** stage 3

#### C3a · URL — Browser Sandbox *(build first: 2.04 s end-to-end [measured])*

- [ ] Playwright Chromium in a container, `--network=none` + allowlisted egress proxy
- [ ] Capture: redirect chain, all requests incl. POST bodies, forms, hidden iframes, screenshot
- [ ] Signals: `credential_post_to_foreign_origin`, brand-keyword-in-title vs. registrable-domain mismatch, `tel:`/`wa.me` anchors → phone numbers straight from the DOM
- [ ] **Never resolve an attacker-chosen URL from the host network** — SSRF into cloud metadata
- [ ] Screenshot surfaced to the user as evidence

#### C3b · APK — Static Triage *(build first: 80 ms [measured])*

- [ ] **Androguard 4.1.4, pinned.** Manifest, permissions, components, certificate, DEX list
- [ ] Signals: `SYSTEM_ALERT_WINDOW` (overlay), `BIND_ACCESSIBILITY_SERVICE`, `RECEIVE_SMS`/`READ_SMS`, `REQUEST_INSTALL_PACKAGES`, `RECEIVE_BOOT_COMPLETED`
- [ ] **Certificate forensics — highest value per line:** self-signed `CN` with `OU=Unknown`, v1-only signature (sideloaded), 25-year validity, debug flag
- [ ] DEX string mining **gated behind manifest triage** (12.4 s / 583 MB): URLs, IPs, `DexClassLoader`, UPI handles, Telegram bot tokens
- [ ] ⚠️ `get_strings()`/`get_classes()` return **filter objects** — `len()` raises. `get_strings()` yields `str` not `bytes`. Call `logger.remove()` or stdout floods
- [ ] ⚠️ Never inline `AnalyzeAPK` (44.9 s / **1.79 GB**) — queue only
- [ ] ⚠️ Phone-number regex on DEX strings is mostly noise — 1,345 false candidates from a legitimate app. Validate format before trusting
- [ ] **Dynamic analysis → delegate** to Hybrid Analysis / Google Triage. Treat the report as a *third-party observation* — a stronger evidence class, because Vouch never touched the payload

#### C3c · Document *(build: <100 ms, pure Python)*

- [ ] Zip-part scan for `word/vbaProject.bin` / `xl/vbaProject.bin` — **definitionally an attack**. Do this **first**
- [ ] `oletools.VBA_Parser` → `detect_vba_stomping()` (source/P-code mismatch hides payloads from static inspection)
- [ ] `pikepdf`/`pypdf` metadata provenance: author, producer, creation tool, date
- [ ] Incremental-update indicator: `raw.count(b"startxref") > 1`, `/Prev` in trailer
- [ ] ⚠️ `oletools` **crashes** on malformed VBA containers — try/except, always after the zip scan
- [ ] ⚠️ A malformed update **breaks pypdf and pikepdf outright** — parser must be fault-tolerant, and **parse success is not a cleanliness signal**
- [ ] **No authenticity classifier.** AgentForge-Bench: agents verified single-field PDF edits in **81.1%** of cases; cheapest forgery **$0.024**. Report indicators, then cross-check the issuing institution's own channel

#### C3d · Image / Screenshot *(assistive only)*

- [ ] tesseract **with content-region cropping** — un-cropped returns **zero characters** [measured]
- [ ] Crop + invert + threshold + upscale before OCR (~0.4 s/region)
- [ ] **All OCR output marked `approximate`; fuzzy-match only.** Never blocklist-match:
      `+91 98204 11938` → `+91 9620411938` [measured]
- [ ] Surface OCR text **and** the screenshot; let the user confirm
- [ ] QR decode (`pyzbar`) — forwarded UPI QR codes are high-value and low-cost
- [ ] C2PA/SynthID provenance via `c2pa-python` — **positive-only**: "no provenance signal found," never "authentic"

#### Not building

- **APK dynamic sandbox** — no maintained open-source Android sandbox exists (Cuckoo 2.x archived 2022; Cuckoo3 Windows-only; redroid needs non-upstream kernel modules + `--privileged`). Emulator-detection defeats the signal anyway.
- **AI-generated content detection** — RoBERTa matches specialised detectors; Hindi degrades worst (IndicDetect); 98.5% evasion achievable (SHADE); 21–35% FP on authentic press photos.
- **Screenshot tamper detection** — no credible library.
- **OpenPhish as primary** — 0% `.in` in the free tier.
- **OTX** — byte-identical output for `google.com` and a nonexistent domain.
- **SearXNG** — hardest to operate, output is attacker-authored prose. Last.

---

### C4 · Accuser & Defender specialist teams *(asymmetric judgment)*

> **v3.3 reshaping.** The collectors in the table below are now part of the **shared
> deterministic fact layer** (code, no LLM, TTL-cached `Fact` records — ARCH §6.10a). The agents
> here are **judgment specialists**: they read facts and emit typed claims. Tool-owned
> **operatives** (URL fetch, delegated APK exec) emit Facts only (rule M10). Cross-team
> duplication of lookups (RDAP, feeds, allowlists) is dissolved by construction.

**Does:** Two teams with **disjoint judgment grants** judge over the shared facts concurrently.
They cannot corroborate each other by construction.

- **Inputs:** `InvestigationPlan` + shared `Fact` records + own disjoint claims
- **Outputs:** `EvidenceClaim[]` to the blackboard, each tagged `agent_role`
- **Side effects:** none directly — fact layer handles outbound HTTPS
- **Pipeline position:** stage 4

| | **Accuser team** (find fraud) | **Defender team** (find legitimate reading) |
|---|---|---|
| Specialists | Feed Analyst · Number Forensic · Payload Analyst | Institutional Analyst · Payment Rail Analyst · Number Legitimacy Specialist |
| Operatives | URL fetch (`url_content`) · delegated APK exec (`apk_dynamic`) | — |
| Emits | `threat_feed_hit` · `number_forensics` · `apk_static` | `institution_legitimacy` · `payment_rail` · `number_legitimacy` |
| Fact layer feeds | GSB, Phishing Army, PhishTank, URLhaus, ThreatFox, SearXNG | Razorpay IFSC + RBI regex, RDAP, institutional allowlist, number class |

- [ ] Concurrent; each writes only to the blackboard
- [ ] **Neither reads the other's claims before writing its own** — enforce in the blackboard API
- [ ] Only deterministic collectors set `verification = verified`
- [ ] Tag every claim with `agent_role` — attribution is the evaluability strategy
- [ ] Feed-miss reported as `NOT_IN_FEED`, **never `SAFE`**
- [ ] Respect branch + budget ceilings

---

### C5 · Dynamic Blackboard

**Does:** The shared typed state that makes this multi-agent rather than two scripts. Accumulates
claims and contradictions across rounds.

- **Inputs:** claims from C3/C4, hypotheses from C6
- **Outputs:** `BlackboardState { claims[], facts[], contradictions[], round, attempts[], standing_instructions[], unresolved[] }`
- **Side effects:** none
- **Pipeline position:** stages 4–7, the spine

- [ ] Closed `Literal` claim types; **scalars only** — free text type-excluded, not discouraged
- [ ] **v3.3:** `facts[]` shared read-only by all specialists (M10); `attempts[]` +
      `standing_instructions[]` append-only; nothing retires silently (§6.5)
- [ ] Dynamic: contradictions recorded **as data**, across rounds
- [ ] Track unresolved claims so round 2 targets *those*, not everything
- [ ] `retracted` status — a human reversal never corrupts corroborating evidence
- [ ] Query surface the Auditor can read without either agent's framing

---

### C6 · Air-gapped extraction + code gate

**Does:** The only component that sees attacker-controlled text. Extracts structure; cannot verify,
cannot act.

- **Inputs:** `canonical_text`, sandbox output
- **Outputs:** `hypotheses[]` (all `unverified`), `suggested_checks[]` (closed enum, capped)
- **Side effects:** model call only — **no tools, no network, no writes**
- **Pipeline position:** stage 5

- [ ] LLM configured with zero tools and zero network
- [ ] Every hypothesis `unverified` by construction — no promotion path
- [ ] Code gate: allowlist → substring grounding (Levenshtein ≥95) → NLI entailment
- [ ] **Fail-closed:** `substring_match_passed` / `nli_entailment_passed` have **no defaults**
- [ ] Log raw model output every run

---

### C7 · Contradiction Auditor *(Agent 3)*

**Does:** Reads the blackboard cold and identifies where Accuser and Defender claims conflict.

- **Inputs:** `BlackboardState`
- **Outputs:** `contradictions[]` + a targeted re-gathering directive
- **Side effects:** none — proposes, Jev disposes
- **Pipeline position:** stage 6

- [ ] Reads **without** either agent's stated conclusion
- [ ] Separate genuine contradiction from apparent disagreement (different scopes, same direction)
- [ ] Emit a **targeted** directive: "resolve claim X vs Y"
- [ ] No tools, no network

> Not a function. Giving the Adjudicator both contradiction-finding *and* the verdict leaves only its
> own robustness between a confident wrong claim and `CONFIRMED_SCAM`.

---

### C8 · Jev Adjudicator + budget authority *(Jev roles #2, #3)*

- **Inputs:** `BlackboardState`, Auditor's directive, C1 Inquest directives
- **Outputs:** `Verdict { label, confidence, citations[], analysis_depth, evidence_classes, contradicted[] }`
- **Side effects:** may commission one bounded re-gathering round
- **Pipeline position:** stage 7

- [ ] Deterministic scoring first; LLM only where rules cannot resolve
- [ ] Reasons over `verified` claims only — the capability invariant
- [ ] **No numeric aggregation across agents** — see the objection below
- [ ] Convergence: stop when Accuser and Defender agree on unrefuted claims
- [ ] Disagreement: `round < 2 AND contradictions` → commission targeted re-gather
- [ ] `NEEDS_MORE_EVIDENCE` as a first-class outcome — never silently "clean"
- [ ] Modality-aware confidence: `static_only` APK ≠ live-feed verdict ≠ OCR-derived

> **Answering the correlated-beliefs objection.** arXiv:2605.18858 shows individually-calibrated
> agents with correlated beliefs become *collectively* miscalibrated — FNR Price of Anarchy up to
> **7.25×** at n=5. Jev never averages confidences; adjudication is sufficiency-and-convergence over
> typed claims. There is no Brier aggregation to get wrong. If a future change adds numeric
> aggregation, this objection becomes live again.

---

### C9 · Dossier & Dispatch

- **Inputs:** `Verdict` + `BlackboardState`
- **Outputs:** human-readable dossier; dispatch to Telegram/WhatsApp/REST
- **Side effects:** outbound provider API
- **Pipeline position:** stage 8

- [ ] Every claim attributed to its agent + provenance — no bare assertions
- [ ] Show contradictions, including unresolved
- [ ] Show `analysis_depth` and `evidence_classes` so confidence is interpretable
- [ ] Show what evidence is *missing* when abstaining
- [ ] Idempotent dispatch: `UNIQUE (submission_id, dispatch_stage)`
- [ ] `XAUTOCLAIM` in the consumer for PEL recovery

---

### C10 · Audit chain

- **Inputs:** ordered claim payloads → **Outputs:** running chain hash
- **Side effects:** append-only writes
- **Pipeline position:** stages 4–8

- [ ] `h_n = SHA256(payload_n ‖ h_{n-1})`
- [ ] Label accurately: **linear hash chain, not Merkle**; **tamper-evident, not tamper-resistant**
- [ ] No compliance claims without external head anchoring

---

## 3. Parallel track — Evaluation

- **Inputs:** labelled corpus + full pipeline traces
- **Outputs:** per-stage metrics, ablation table, **attribution metrics**, cost-per-verdict
- **Side effects:** reads corpus; writes reports
- **Pipeline position:** observing

- [ ] Reproduce the **F1 0.715** deterministic control (ablation floor, not a goal)
- [ ] **Attribution metric — the novel one:** which agent produced the decisive claim? Does the dossier attribute it correctly? Nobody publishes this, and it is how you test whether the asymmetry earns its cost
- [ ] Ablation: deterministic-only │ Accuser-only │ Defender-only │ both │ both+Auditor │ +extractor
- [ ] Per-modality metrics: URL, APK, document, OCR (reported separately — they are not comparable)
- [ ] **Adversarial suite** (~20 hostile messages) written **before** C4: attempt to steer `suggested_checks`, flood the blackboard, induce a confident false verdict
- [ ] Cost per verdict: model calls, outbound HTTP, browser seconds
- [ ] Gate every component on a metric

### Corpus reality

| Limitation | Consequence |
|---|---|
| **0 URLs, 0 domains, 0 IFSCs** in the current corpus [measured] | Feed collectors and browser sandbox are **unevaluable here** |
| Mozambican M-Pesa numbers | Telecom checks correctly return no signal |
| Binary labels only, no entity ground truth | Per-claim scoring impossible; only end-to-end |
| Not the deployment language | Indian-smishing claims unsupported |

**Action with the highest value:** contact **Shaghayegh Hosseinpour**, first author of POSTER
(arXiv:2505.18233, 84,882 messages / 24,086 smishing) — the same author is attached to the
`Smishing_Dataset` repo that is currently unretrievable. That single email could unblock the
evaluation story.

---

## 4. Build order

Fast, lean, nothing major dropped. Each step runs.

| # | Step | Done when |
|---|---|---|
| 1 | **C0 fixes** | Webhooks stop 500-ing; `submission_id` flows into the stream |
| 2 | **C1 canonicalizer** | Confusables folded; entities extracted; prefix-obfuscation handled |
| 3 | **C5 blackboard** | Schema validates; free text rejected; contradictions recordable |
| 4 | **C3a URL browser sandbox** | A phishing URL yields credential-post + brand-mismatch + screenshot |
| 5 | **C3b APK manifest triage** | A forwarded APK yields permissions, cert tells, verdict — **80 ms** |
| 6 | **C4 + C8 deterministic** | End-to-end verdict from real feeds, no LLM. Attribution recorded |
| 7 | **C9 + C10** | Dossier prints with agent attribution; audit chain works |
| 8 | **Eval harness** | F1 0.715 control + attribution metric running |
| 9 | **C2 Jev Inquest** | Plans before gathering; fast-path bypasses the LLM |
| 10 | **C7 Auditor** | Contradictions found by independent read; targeted re-gather works |
| 11 | **C6 extraction + gate** | Air-gapped worker emits unverified hypotheses; cannot promote |
| 12 | **Ablation** | Which agent produced the decisive evidence? |
| 13 | **C3c document** | Macro + structure indicators; parser survives malformed input |
| 14 | **C3d image/OCR** | Fuzzy-matched OCR; QR extraction; C2PA provenance |
| 15 | **HITL loop** | Human review → signed label → retraction path |
| 16 | **v3.3: thread intake + SMD/CSD** | `kind=thread` accepted; SMD runs on every submission, CSD on threads only (rolling summary, adaptive cadence); verdict threat level = OR across paths |
| 17 | **v3.3: Advisor** | `attempts[]` recorded; round-boundary single pass emits append-only instruction deltas; grants/budget never altered |
| 18 | **v3.3: advice tail** | Advice Writer prose passes the Narrative Verifier; any ungrounded assertion falls back to the deterministic template |

Every step from 1 produces a working system. **No step drops a component** — steps 9–12 add the
agentic layer onto a pipeline that already runs, and steps 16–18 add the v3.3 layer (thread
intake, Advisor, advice tail) onto that.

---

## 5. Explicitly not building

| Not building | Why |
|---|---|
| Self-hosted APK dynamic sandbox | No maintained OSS Android sandbox. Delegate to hosted sandboxes |
| AI-generated content detection | Detectors degrade under shift; 98.5% evasion achievable; 21–35% FP on real photos |
| Screenshot tamper detection | No credible library |
| PDF authenticity classifier | Forgery is $0.02 at 81% success. Indicators + institutional cross-check instead |
| OTX | Byte-identical output for known-good and unknown domains |
| OpenPhish as primary feed | 0% `.in`; superseded by Safe Browsing `SOCIAL_ENGINEERING` |
| UPI VPA payee lookup | Licensed aggregator — procurement, not engineering |
| TRAI/DLT registry lookup | No public API exists; heuristic over number class only |
| Numeric confidence aggregation | Price-of-Anarchy objection ([§C8](#c8--jev-adjudicator--budget-authority-jev-roles-23)) |

**Note what is *not* on this list:** the multi-agent DAG, dynamic blackboard, Jev's three roles,
browser sandbox, OCR, document forensics, queue, API, database. All present.

---

## 6. Open questions

| # | Question | Blocks | Resolution |
|---|---|---|---|
| Q1 | Which agent produces the decisive evidence? Does the asymmetry earn its cost? | Justification for the DAG | Attribution metric, step 12 |
| Q2 | Can untrusted text steer `suggested_checks` or flood the blackboard? | Trust in C4/C6 | Adversarial suite before step 10 |
| Q3 | Can the corpus be unblocked? | All per-claim evaluation | Email POSTER's author |
| Q4 | VT free API licensing — commercial use? | Production dependency | Read the terms before depending on VT |
| Q5 | Does the browser sandbox change verdicts vs. feed lookup alone? | Whether C3a is worth 2 s | Compare cohorts |

---

<a id="anchor-measurements"></a>
## Measurements

All locally reproduced; none on trust.

| Measurement | Value |
|---|---|
| Deterministic control | F1 0.715 (P 0.688, R 0.745), no LLM, no network |
| APK manifest triage | **80 ms** |
| APK DEX parse | 12.4 s / 583 MB |
| APK `AnalyzeAPK` | 44.9 s / **1.79 GB** |
| URL browser analysis | **2.04 s** end-to-end incl. screenshot |
| OCR (cropped region) | ~0.4 s; digit errors silent and unavoidable |
| Phishing Army blocklist | 144,715 entries, live |
| Corpus entity yield | 0 URLs, 0 domains, 0 IFSCs |
| NFKC vs confusables | folds fullwidth ✓, `ﬁ` ✓; Cyrillic `а` / Greek `ο` survive |
| OTX | byte-identical for known-good and nonexistent domains |
