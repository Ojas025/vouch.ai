# Tool Design

The model-facing tool surface for Vouch's agentic investigation runtime.

This document records, for each tool, **what it is** and **when a model uses it**. Argument
and response schemas are deliberately not included here — they are decided separately.

Conventions referenced throughout:

- **Scope.** Every investigative tool reads only what the task envelope's `scope_refs`
  permit. The model cannot widen its own scope by argument.
- **Trust.** All output carries the `trust` levels defined in
  `specs/trust-provenance-identity.md`. Network-served content is always
  `UNTRUSTED_ARTIFACT`.
- **Artifacts are handles.** Tools exchange artifact identifiers, never filesystem paths.
- **Citation.** Tool results are citable only through the ledger evidence ids they
  produce. Search snippets and model outputs are never evidence.

The twelve tools are grouped into six families: ledger read, entity intelligence,
web and infrastructure, artifact analysis, memory, and decision.

---

## 1. Ledger read

### 1.1 `evidence_query`

**Definition.** The single bulk read path into the investigation ledger. Returns a bounded,
role-filtered, scope-bound slice of evidence records, with direct-fetch-by-id and
filtering by kind, entity, modality, text, trust level, or sequence position.
Role filtering is enforced server-side: a `WORKER` cannot see another task's hypotheses,
while `VERIFIER` and `SYNTHESIS` additionally see claims and verification events.

**When the model uses it**

- As the first call of a task, to establish what is already known before spending any
  network or analysis budget.
- Before making an external tool call, to check whether the fact it needs is already
  recorded — the cheapest evidence is the evidence that already exists.
- To fetch specific records by id after `evidence_trace` has named them.
- To page through its scope and assess coverage before asserting a hypothesis.

### 1.2 `evidence_trace`

**Definition.** Expands the evidence graph from an anchor — an evidence record, a claim,
or a typed entity — following support, contradiction, supersession, and shared-entity
relations. Answers *"why do we believe this?"* and *"what disagrees with this?"*.
Traversal is bounded by depth and node count, never crosses outside `scope_refs`, and a
worker's trace cannot include hypotheses from other tasks.

**When the model uses it**

- A verifier ruling on a claim needs the exact subgraph the claim rests on, rather than
  reading every record in scope.
- A worker wants to know whether its hypothesis has already been refuted somewhere in the
  case before re-deriving it.
- Following a shared identifier (`cert_sha256`, `favicon_sha256`, phone number, wallet
  address) from one artifact to every other artifact in the case that shares it.
- Hunting for contradictions: source disagreement, stale-versus-fresh conflicts on the
  same entity, or records that supersede one another.

---

## 2. Entity intelligence

### 2.1 `entity_intel`

**Definition.** Looks up a single typed indicator against the threat-intelligence and
reference sources that are valid for its type, and returns a normalised, comparable
per-source verdict. Absorbs the seven separate fact-source tools (URLhaus, VirusTotal,
PhishTank, AbuseIPDB, IBM X-Force, Google Safe Browsing, IFSC validation) behind one
contract: the model names an entity, the runtime dispatches only to sources that cover
that type, and the result preserves per-source provenance and epistemic state —
"queried, nothing listed" and "source has no coverage" remain distinguishable.
`ALLOWLIST` is a source like any other.

**When the model uses it**

- Domain, URL, file-hash, or IFSC reputation before asserting that an indicator is
  malicious — the model must not assert malice on its own reading of a page.
- Re-checking a fact whose freshness requirement the ledger has flagged as stale.
- Cross-checking whether two sources agree or disagree about the same entity.

It is *not* used for infrastructure analysis (DNS, certificate transparency, registrant,
typosquat) — that belongs to `url_trace` (§3.2) — nor for reading page content, which
belongs to `web_fetch` (§3.4).

---

## 3. Web and infrastructure

### 3.1 `web_search`

**Definition.** Searches the public web for pages about an indicator, brand, campaign, or
phrase, returning ranked titles, URLs, and snippets — never page content. Every hit is
treated as `UNTRUSTED_ARTIFACT`; snippets are attacker-influenceable through SEO and are
recorded as observations, never interpreted as instructions.

**When the model uses it**

- Establishing whether a brand actually uses a given domain or payment handle, and finding
  the official domain to compare against the submitted one.
- Campaign discovery: whether an "SBI reward" or "India Post parcel fee" scheme has been
  reported before.
- Checking whether a phone number, UPI handle, or URL has been reported publicly.
- Choosing a target for `web_fetch` — snippets are sufficient to decide what is worth
  rendering.

### 3.2 `url_trace`

**Definition.** Resolves where a link actually leads and who operates the terminal
infrastructure: the redirect chain, shortener expansion, and — on the final host — DNS,
certificate transparency, RDAP registrant, and typosquat distance. Every hop is
re-validated against private, link-local, and metadata address ranges before any request
is made; a policy block is reported explicitly rather than as an empty result. Follows
redirects without executing JavaScript.

**When the model uses it**

- The message contains a shortened or visibly truncated link, or the anchor text
  disagrees with the href — a phishing indicator on its own.
- The investigation needs registrant age, CT sibling counts, or brand-typosquat distance.
- The redirect chain itself is the signal — for example shortener → cloaker → landing.

It is *not* used for rendering the page; `web_fetch` (§3.4) owns that.

### 3.3 `web_fetch`

**Definition.** Loads one URL in a sandboxed browser and captures what the host serves
*now* — including JavaScript redirects, form actions, third-party scripts, wallet
addresses, and a screenshot. Distinct from `url_trace` in that it executes and renders;
distinct from existing third-party scan records (urlscan, VirusTotal) in that the capture
is contemporaneous rather than someone else's historical scan. Screenshot bytes are
returned as an artifact handle; if the bound agent profile declares a `vision` capability
the image is attached, otherwise the runtime extracts text from it instead — one tool,
capability-dependent delivery.

**When the model uses it**

- The URL resolved but the question is what it serves *at investigation time*, not what a
  shared scan saw days ago.
- The page matters visually: bank logo, login form, payment QR code.
- Form actions, third-party scripts, or wallet addresses are needed.
- A JavaScript redirect must be rendered to reach the true landing page.

### 3.4 `artifact_extract`

**Definition.** Deterministic extraction from documents and images — PDFs, images, Office
files, screenshots. Covers text, OCR, QR and barcode payloads, embedded metadata, embedded
files, macros, extracted URLs, logo similarity, and perceptual hashing. Extraction is
deterministic and repeatable; newly discovered typed entities (a QR payload containing a
URL, for instance) are returned so they can enter the evidence graph and, if warranted,
spawn follow-on tasks.

**When the model uses it**

- The submission carries an image, PDF, or Office artifact and the model needs its content.
- A screenshot likely contains a QR code or payment handle.
- A PDF may conceal links, JavaScript, macros, or embedded files.
- Logo or brand similarity, or a perceptual hash for clone detection, is the question.

It is *not* used for APK contents — that is the `apk_*` family (§4).

---

## 4. Artifact analysis (APK)

The APK tools query a deterministic representation built once per file identity
(apktool, jadx, androguard, APKiD, Quark, and YARA) and shared across all of them. They
are views over one artefact, not four independent analyses, so a code finding and a
profile finding about the same package cannot disagree.

### 4.1 `apk_profile`

**Definition.** Declared identity and static posture of an APK: package and app identity,
version and SDK targets, dangerous permissions, exported components, declared-intent
mismatches, signing certificate, publisher match against the brand index, packing
detection, Quark behaviour rules, YARA signatures, and native libraries. When packing
detection hides code, the tool reports coverage as *incomplete* rather than presenting a
partial static picture as complete.

**When the model uses it**

- As the first call on any APK — always, before any code-level question.
- The app claims to be a bank, wallet, or government service and the model needs the real
  package and certificate to compare.
- The model must know whether the APK is packed *before* trusting any code-level finding.
- Permission posture is the question: SMS, accessibility, overlay, install-packages,
  device-admin.

### 4.2 `apk_code`

**Definition.** Locates APIs, strings, methods, or classes in the decompiled representation
and returns bounded context around each hit — a source window, optionally callers and
callees, optionally scoped to a package. Matches are returned in terms of the
representation version so a result is reproducible. When the profile reports packing, the
result carries a coverage warning rather than an unqualified hit list.

**When the model uses it**

- The profile flagged a risky capability and the model needs the code that exercises it.
- Searching for a hardcoded C2 host, wallet address, or credential string.
- Locating a method before asking `apk_flow` a path question about it.
- Reading a short method body directly.

### 4.3 `apk_flow`

**Definition.** Computes one program-flow relation — reachability, taint, or program slice
— between two points in the APK's call and data-flow graph. Reports the engine used, how
many entry points were considered, and whether the result is exact, approximate, or
unsupported for the requested mode, so the model never reads an approximate path as a
proven one.

**When the model uses it**

- "Can this exported receiver actually reach `SmsManager.sendTextMessage`?"
- "Does the SMS body flow to a network sink?" — credential or OTP exfiltration.
- "Give me the minimal slice around this crypto call" — removing injected decoy code.

It is *not* used for finding the method in the first place; `apk_code` (§4.2) does that.

---

## 5. Memory

### 5.1 `memory_recall`

**Definition.** Retrieves precedent from past investigations that were closed and
**verified**: similar cases, their verdicts, and the tool trajectories that resolved them.
Retrieval is tiered by what past content the profile is cleared to see. Memory results are
foreign to the current case — the identifiers they carry belong to the prior case and are
never citable as local evidence, and even a full-tier match still requires fresh
verification in this investigation. Only verified episodes enter the index by default.

**When the model uses it**

- An APK family, domain pattern, or campaign looks recurring and the model wants prior
  outcomes rather than rediscovering them.
- Choosing which investigative path is worth the budget on a hard artifact, by seeing which
  tool sequence resolved comparable cases.
- Checking whether a near-identical prior case ended inconclusive, and why.

---

## 6. Decision

### 6.1 `decide`

**Definition.** Asks a calibrated, fast (System-1) decision model — Jev, or its open
Laya equivalent — a fixed, versioned set of typed questions about a state assembled from
ledger evidence records. The question set is owned by the registry, not the model; the
decision model returns a calibrated choice, score, or uncertainty value, never prose or a
rationale. Its output is `MODEL_OUTPUT`: a signal the verifier weighs, never a finding,
never an observation, never citable.

**When the model uses it**

Available to the **verifier and synthesis** profiles only, never to a worker — a verdict
oracle bound to a worker ends the investigation before the evidence does.

- Ruling on a claim: supported, insufficient, or contradicted, with calibrated probability.
- Recalibrating severity after new evidence lands.
- Judging whether a follow-up round would close a specific evidence gap.
- Comparing an APK's declared intent against its requested permissions.

---

## Cross-cutting rules

1. **Scope is bound, never an argument.** No tool lets the model widen its own scope.
2. **Every investigative tool writes before it answers.** Observations enter the ledger
   through the single writer and the model cites what it was given.
3. **Search snippets are never evidence.** Only ledger-backed records may appear in
   `evidence_refs`.
4. **Memory is precedent, not evidence.** Prior-case records cannot substitute for
   verification in the current case.
5. **`decide` outputs are signals, never findings.** They are `MODEL_OUTPUT` and always
   lose ties to `VERIFIED` evidence.
6. **Coverage is reported, never assumed.** Packed APKs, unsupported analysis modes, and
   intelligence sources without coverage are stated as such rather than silently omitted.
