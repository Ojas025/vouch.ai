# Spec: Trust, Provenance & Identity

- **Prefix:** `TRU`
- **Status:** `approved`
- **Interface:** See below
- **Test locations:** unit: `tests/v1/unit/test_trust_contracts.py`, `test_provenance.py`, `test_identity.py`; integration: `tests/v1/integration/test_ledger_trust.py`, `test_tool_trust_policy.py`; e2e: not applicable for this foundation contract

## Purpose

Define the trust, provenance, and identity contract shared by every ledger record, tool call, and model output.

`TRUSTED` means **safe to use as a control input**: a value that may determine where a request goes, who receives a message, which path is written, or which artifact is executed. It does not mean true, benign, or evidentially strong. All content derived from a submitted message, artifact, external tool, or model is `UNTRUSTED` for control purposes. Evidence strength is expressed through provenance, source, confidence, and verification status, not through the trust label.

The contract must make these invariants true:

1. No submitted, external, or model-derived content can become `TRUSTED` by composition, confidence, verification, or model self-assessment.
2. A privileged action cannot be driven by untrusted control inputs unless an explicit code-declared policy permits it.
3. Every record traces to its origin, producer, versions, parent reads, and source bytes.
4. Trust and provenance survive worker, process, and session boundaries.

## Interface

```python
class TrustLevel(str, Enum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"

class Origin(str, Enum):
    SYSTEM = "system"
    USER_ARTIFACT = "user_artifact"
    TOOL = "tool"
    MODEL = "model"
    PIPELINE = "pipeline"

class ParameterRole(str, Enum):
    CONTROL = "control"
    DATA = "data"

class ParameterPolicy(BaseModel):
    role: ParameterRole
    accepts_untrusted: bool = False  # control: only with constrained_fetch

class Provenance(BaseModel):
    origin: Origin
    producer_id: str
    task_id: str | None = None
    parent_refs: list[str] = Field(default_factory=list)
    citation_refs: list[str] = Field(default_factory=list)
    read_set_id: str | None = None
    label_policy_version: str
    tool_name: str | None = None
    tool_version: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    sandbox_id: str | None = None
    artifact_sha256: str | None = None
    content_locator: str | None = None
    observed_at: datetime

class LabeledValue[T](BaseModel):
    value: T
    trust: TrustLevel
    provenance: Provenance

class ReadPurpose(str, Enum):
    CONTENT = "content"
    CITATION = "citation"

class ReadSet(BaseModel):
    read_set_id: str
    task_id: str
    content_record_ids: list[str]
    citation_record_ids: list[str]

class EntityRef(BaseModel):
    type: EntityType
    value: str

class TextSpan(BaseModel):
    start: int
    end: int

class ExtractedEntity(BaseModel):
    type: EntityType
    normalized_value: str
    extracted_value: str
    source_field: Literal["text", "ocr", "qr", "image"]
    span: TextSpan | None = None
    extractor_name: str
    extractor_version: str
    normalizer_name: str
    normalizer_version: str
    confidence: float = Field(ge=0.0, le=1.0)

class ArtifactRef(BaseModel):
    sha256: str  # 64 lowercase hexadecimal characters
```

`ToolDefinition` adds:

```python
parameters: dict[str, ParameterPolicy]
deterministic: bool
network_access: bool
constrained_fetch: bool = False
```

`ToolCallRequest.arguments` is `dict[str, LabeledValue[Any]]`. `ToolFailureReason` adds `UNTRUSTED_INPUT = "untrusted_input"` and `AUDIT_UNAVAILABLE = "audit_unavailable"`.

Public errors: `TrustLabelMissing`, `TrustPromotionError`, `EmptyJoinError`, `MissingParent`, `UnknownParent`, `ProvenanceCycle`, `ImmutableRecordError`, `UntrustedContentInTrustedRecord`, `ScopeViolation`, `UnreportedParent`, `MissingArtifactRef`, `MissingContentLocator`, `InvalidParameterPolicy`, `ReadSessionMismatch`, `FetchBlocked`, `AuditUnavailable`.

## Requirements

### `TRU-001` Single integrity label

- **Behavior:** Every trust-bearing value or record carries exactly one `TrustLevel`, either `TRUSTED` or `UNTRUSTED`. No other value is accepted, and the field has no default.
- **Approval:** `approved`
- **Verification level:** `unit`
- **Invariants:** Valid labels are exactly `{TRUSTED, UNTRUSTED}`. A missing label on an internally constructed value raises `TrustLabelMissing`. `TrustLabelMissing` is a domain error, not a silently applied default.
- **Errors and edge cases:** External adapters fill `UNTRUSTED`. Internal constructors reject a missing label. Explicit `None` and unknown strings raise `ValidationError`. No model field named `trust` may have a default.

| Given / Input | When | Expected result |
|---|---|---|
| System policy record with `trust=TRUSTED` | Validate | Accepted |
| Submitted-message record with `trust=UNTRUSTED` | Validate | Accepted |
| Record with `trust="deterministic"` | Validate | `ValidationError` |
| Record with no trust field | Validate | `TrustLabelMissing` |
| Model definition `trust: TrustLevel = TRUSTED` | Introspect | Test failure: no default allowed |

### `TRU-002` External and model content is never trusted

- **Behavior:** Content entering from a channel, tool, or model is `UNTRUSTED`. An adapter may assign `TRUSTED` only to a value it constructs from developer-controlled data under an approved policy. It may not assign `TRUSTED` to content derived from external or model output.
- **Approval:** `approved`
- **Verification level:** `unit`
- **Invariants:** No external, tool, or model content is `TRUSTED` by default. No adapter promotes external or model content. Only a `SYSTEM` value or a local deterministic path with all-trusted inputs may be `TRUSTED`.
- **Errors and edge cases:** At an internal trust boundary, an unlabelled typed value raises `TrustLabelMissing`. An adapter that assigns `TRUSTED` to external or model content raises `TrustPromotionError`.

| Given / Input | When | Expected result |
|---|---|---|
| Unlabelled network-tool output | Ingest | `UNTRUSTED` |
| Unlabelled model response | Ingest | `UNTRUSTED` |
| Unlabelled submitted text | Ingest | `UNTRUSTED` |
| Adapter constructs a system timestamp from code | Ingest | May be `TRUSTED` |
| Adapter assigns `TRUSTED` to submitted text | Ingest | `TrustPromotionError` |
| Unlabelled value passed to a privileged tool | Validate the call | `TrustLabelMissing` |

### `TRU-003` Trust combines by join

- **Behavior:** When values or records are combined, the result is `UNTRUSTED` if any content input is `UNTRUSTED`; it is `TRUSTED` only when every content input is `TRUSTED`.
- **Approval:** `approved`
- **Verification level:** `unit`
- **Invariants:** Join is commutative, associative, and idempotent over non-empty label sets. `UNTRUSTED` is absorbing. A derived record has at least one content parent; a source record’s label is assigned by its origin, not by an empty join.
- **Errors and edge cases:** Calling join with zero labels raises `EmptyJoinError`. Appending a derived record with no content parents raises `MissingParent`.

| Given / Input | When | Expected result |
|---|---|---|
| `TRUSTED` and `TRUSTED` | Join | `TRUSTED` |
| `TRUSTED` and `UNTRUSTED` | Join | `UNTRUSTED` |
| `UNTRUSTED` and `UNTRUSTED` | Join | `UNTRUSTED` |
| No labels | Join | `EmptyJoinError` |
| Derived record with no content parents | Append | `MissingParent` |

### `TRU-004` Model output is always untrusted

- **Behavior:** Every value produced by a language model is `UNTRUSTED`, regardless of prompt labels, evidence labels, output schema, confidence, or model self-assessment.
- **Approval:** `approved`
- **Verification level:** `unit`
- **Invariants:** No model-origin record is `TRUSTED`. A writer that declares `TRUSTED` for `origin=MODEL` is rejected.
- **Errors and edge cases:** Declaring `TRUSTED` for a model output raises `TrustPromotionError`. Valid JSON, a bounded enum, or a high confidence score does not change the label.

| Given / Input | When | Expected result |
|---|---|---|
| Model summary of a `TRUSTED` system prompt | Write | `UNTRUSTED` |
| Model extraction from `UNTRUSTED` message text | Write | `UNTRUSTED` |
| Model output validating against a JSON enum | Write | `UNTRUSTED` |
| Writer declares `trust=TRUSTED`, `origin=MODEL` | Validate | `TrustPromotionError` |

### `TRU-005` Tool output is the join of its inputs

- **Behavior:** A tool output’s trust is the join of its content inputs, content reads, and control arguments. If the tool has `network_access=True` or `deterministic=False`, its output is `UNTRUSTED`. A deterministic, local tool with all-trusted inputs may produce `TRUSTED`. A tool with no content inputs is a source record; it is `TRUSTED` only when `deterministic=True` and `network_access=False`.
- **Approval:** `approved`
- **Verification level:** `unit`, `integration`
- **Invariants:** No tool output is above the join of its inputs. Network and external tools never produce `TRUSTED`. A tool cannot statically declare a higher output trust.
- **Errors and edge cases:** A tool that returns a label above the computed join raises `TrustPromotionError`. A tool declaration that attempts to fix `output_trust=TRUSTED` is rejected as an invalid declaration.

| Given / Input | When | Expected result |
|---|---|---|
| URLhaus lookup over a domain | Write output | `UNTRUSTED` |
| Local SHA-256 over a `TRUSTED` file | Write output | `TRUSTED` |
| Local SHA-256 over an `UNTRUSTED` APK | Write output | `UNTRUSTED` |
| Local regex over `UNTRUSTED` text | Write output | `UNTRUSTED` |
| Deterministic local tool with no content inputs | Write output | `TRUSTED` |
| Tool returns `TRUSTED` with an `UNTRUSTED` content input | Validate | `TrustPromotionError` |

### `TRU-006` Verification never promotes trust

- **Behavior:** A record’s trust label is immutable after write. Verification appends a `VerificationEvent`; it does not modify the hypothesis or evidence and does not raise the label. A lifecycle status such as `verified` is separate from trust and is never read by the join. A superseding record is a new record; it does not promote the superseded one.
- **Approval:** `approved`
- **Verification level:** `unit`, `integration`
- **Invariants:** No update changes `trust` or `provenance`. A `VerificationEvent` citing an `UNTRUSTED` hypothesis is `UNTRUSTED`. Readers may select the latest record in a supersession chain but must not infer a higher trust.
- **Errors and edge cases:** Updating a stored label raises `ImmutableRecordError`. Re-extraction appends a new record; it never mutates the old one.

| Given / Input | When | Expected result |
|---|---|---|
| `UNTRUSTED` hypothesis is supported | Append verification | Hypothesis unchanged; event appended |
| Hypothesis status becomes `verified` | Read | `trust` remains `UNTRUSTED` |
| Stored hypothesis updated with a new label | Update | `ImmutableRecordError` |
| Replacement record supersedes an old one | Append | New record has its own label; old record unchanged |

### `TRU-007` Per-parameter sink policy

- **Behavior:** Every tool parameter is declared `control` or `data`. A control parameter determines a privileged effect and defaults to accepting only `TRUSTED`. A data parameter is content and defaults to accepting only `TRUSTED`; it may set `accepts_untrusted=True`. A control parameter may set `accepts_untrusted=True` only when the tool declares `constrained_fetch=True` and satisfies `TRU-020`. The tool must also pass its existing capability, effect-class, and network checks. Defaults are deny.
- **Approval:** `approved`
- **Verification level:** `unit`, `integration`
- **Invariants:** Every argument field of `args_model` has exactly one `ParameterPolicy`. No undeclared argument is accepted. `accepts_untrusted=True` on a control parameter without `constrained_fetch=True` is invalid. Policy is code-declared; a model cannot grant it. A denied call dispatches no tool code and produces no side effect.
- **Errors and edge cases:** An untrusted control argument returns `DENY` with `UNTRUSTED_INPUT`. An untrusted data argument with `accepts_untrusted=False` returns `UNTRUSTED_INPUT`. A missing or extra parameter policy raises `InvalidParameterPolicy`.

| Given / Input | When | Expected result |
|---|---|---|
| `urlhaus.lookup(url)`; `url` is data, accepts untrusted | Authorize | `PERMIT` |
| `fetch_url(url)`; `url` is control, untrusted, `constrained_fetch=False` | Authorize | `DENY`, `UNTRUSTED_INPUT` |
| `fetch_url(url)`; `url` is control, untrusted, `constrained_fetch=True` | Authorize | `PERMIT` only if `TRU-020` passes; otherwise `FetchBlocked` |
| `send_message(recipient, body)`; recipient control trusted, body data untrusted accepted | Authorize | `PERMIT` |
| `sandbox_execute(artifact, profile)`; artifact data untrusted accepted, profile control trusted | Authorize | `PERMIT` |
| Control parameter declares `accepts_untrusted=True` without constrained fetch | Register tool | `InvalidParameterPolicy` |
| Undeclared argument | Validate call | `InvalidParameterPolicy` |

### `TRU-008` Every record carries provenance

- **Behavior:** Every trust-bearing record carries `Provenance` with an origin, a non-empty producer identifier, a `label_policy_version`, and an observed-at timestamp. Origin-specific requirements: `USER_ARTIFACT` requires `artifact_sha256`; `TOOL` requires `tool_name` and `tool_version`; `MODEL` requires `model_id` and `prompt_version`; a derived `PIPELINE` record requires at least one content parent and a `read_set_id`. A record written under a task carries `task_id`.
- **Approval:** `approved`
- **Verification level:** `unit`
- **Invariants:** A record never exists without provenance or `label_policy_version`. Timestamps are timezone-aware and normalised to UTC. Fields that do not apply to an origin are null. Gate, verifier, synthesis, and orchestrator records use `origin=PIPELINE` and a `producer_id` naming the component. `citation_refs` are references, not content dependencies.
- **Errors and edge cases:** A missing origin-specific field raises `ValidationError`. A naive timestamp raises `ValidationError`. A task write without `task_id` raises `ValidationError`. A derived record without `read_set_id` raises `ValidationError`.

| Given / Input | When | Expected result |
|---|---|---|
| System configuration record | Write | `origin=SYSTEM`, producer ID, policy version, UTC timestamp; tool/model fields null |
| Submitted text record | Write | `origin=USER_ARTIFACT`, `artifact_sha256`, no parents |
| Network tool record | Write | `origin=TOOL`, `tool_name`, `tool_version`, `read_set_id`, parent content IDs |
| Model record | Write | `origin=MODEL`, `model_id`, `prompt_version`, `read_set_id`, parent content IDs |
| Gate finding | Write | `origin=PIPELINE`, `producer_id="gate"`, `read_set_id` |
| Model record without `model_id` | Validate | `ValidationError` |
| Derived record without `read_set_id` | Validate | `ValidationError` |
| Task record without `task_id` | Validate | `ValidationError` |
| Naive datetime | Validate | `ValidationError` |

### `TRU-009` Derived records form an acyclic content graph

- **Behavior:** `parent_refs` identify records whose content contributed to a derived record. `citation_refs` identify reference-only links. Only `parent_refs` form the derivation graph and contribute to the join. Every parent resolves; the parent graph is acyclic; the declared trust equals the join of the content parents, subject to `TRU-004` and `TRU-005`.
- **Approval:** `approved`
- **Verification level:** `unit`, `integration`
- **Invariants:** No record is its own ancestor through `parent_refs`. Parent IDs are unique within a record. Source records may have no parents; derived records may not. A `citation_ref` does not affect trust and does not create a derivation edge.
- **Errors and edge cases:** Unknown parent raises `UnknownParent`. A cycle in `parent_refs` raises `ProvenanceCycle`. A missing parent on a derived record raises `MissingParent`. A declared trust above the content join raises `TrustPromotionError`.

| Given / Input | When | Expected result |
|---|---|---|
| Hypothesis with two content parent IDs | Append | Accepted; trust equals join |
| Derived record with no content parents | Append | `MissingParent` |
| Parent ID absent from the ledger | Append | `UnknownParent` |
| A is content parent of B, B is content parent of A | Append | `ProvenanceCycle` |
| Content parent is `UNTRUSTED`, child declares `TRUSTED` | Append | `TrustPromotionError` |
| Child cites `UNTRUSTED` record only in `citation_refs` | Append | Citation does not affect the child’s trust |

### `TRU-010` Read sets are cumulative and purpose-bound

- **Behavior:** A read session is opened for a task and role. Every read declares `purpose=CONTENT` or `purpose=CITATION`. A content read returns the record content and its label; a citation read returns only the record ID and label, never the content. The session accumulates a cumulative read set. `read_set_id` identifies that cumulative set at the moment a derived record is constructed. Multiple reads before that moment are included; reads after it are not. Append binds the record to the token; the ledger computes trust from the content reads in the token, not from self-reported parents. A token from another session or task is rejected.
- **Approval:** `approved`
- **Verification level:** `unit`, `integration`
- **Invariants:** The writer cannot mint, choose, or reuse a token across tasks. A content read cannot be reclassified as a citation read after the fact. `parent_refs` are a subset of the token’s content reads. `citation_refs` are a subset of the token’s citation reads. `task_id` matches the token. Role and scope checks apply to both read purposes.
- **Errors and edge cases:** A token from another task or session raises `ReadSessionMismatch`. An out-of-scope record raises `ScopeViolation`. A `parent_ref` outside the content read set raises `UnreportedParent`. An omitted content read cannot reduce trust because the join uses the full content read set.

| Given / Input | When | Expected result |
|---|---|---|
| Content-read `UNTRUSTED` O, then derive | Append | Child is `UNTRUSTED` |
| Content-read O, then content-read T, then derive | Append | Child is `UNTRUSTED`; both are in the token |
| Citation-read O only, then derive | Append | O is not in the content join; child may be `TRUSTED` |
| Use a token issued for another task | Append | `ReadSessionMismatch` |
| Cite a parent outside the content read set | Append | `UnreportedParent` |
| Cite evidence outside the role scope | Append | `ScopeViolation` |
| Read O but declare only T as a parent | Append | Child still joins O; declaring `TRUSTED` raises `TrustPromotionError` |

### `TRU-011` Trusted records use a closed payload

- **Behavior:** A `TRUSTED` record contains only identifiers, hashes, closed enums, numbers, booleans, and `LabeledValue` values whose trust is `TRUSTED`. It contains no bare free-form `str` or `bytes`. A `TRUSTED` record may cite `UNTRUSTED` records in `citation_refs`, because citation reads return no content. It may embed an `EntityRef` only when every contributing occurrence is `TRUSTED`; otherwise it references the occurrence or record by ID. A system-authored template with placeholders may be `TRUSTED`; its rendered output is `UNTRUSTED` if it interpolates untrusted text.
- **Approval:** `approved`
- **Verification level:** `unit`
- **Invariants:** The trust of a record is the join of its content. A closed payload validator rejects free-form strings and bytes in a trusted record. Untrusted content is referenced, never copied into a trusted record. A citation-only link is not a content dependency.
- **Errors and edge cases:** A `TRUSTED` record with a quoted message raises `UntrustedContentInTrustedRecord`. A trusted record with a `parent_ref` to an `UNTRUSTED` record raises `TrustPromotionError`. A trusted record with an `EntityRef` from an untrusted occurrence raises `UntrustedContentInTrustedRecord`. A `citation_ref` to an untrusted record is accepted.

| Given / Input | When | Expected result |
|---|---|---|
| `TRUSTED` record with a quoted user message | Validate | `UntrustedContentInTrustedRecord` |
| `TRUSTED` record with `citation_refs=[untrusted_id]` | Validate | Accepted |
| `TRUSTED` record with `parent_refs=[untrusted_id]` | Validate | `TrustPromotionError` |
| `TRUSTED` record with an `EntityRef` from an untrusted occurrence | Validate | `UntrustedContentInTrustedRecord` |
| `TRUSTED` template with a placeholder and no user text | Validate | Accepted |
| Rendered template interpolating untrusted text | Render | Output is `UNTRUSTED` |
| `TRUSTED` record with a `bytes` field | Validate | `ValidationError` |

### `TRU-012` Entity identity is separate from occurrence

- **Behavior:** `EntityRef` is exactly `(EntityType, normalized_value)`. Two refs are equal if and only if both fields are equal. `EntityRef` carries no raw value, span, source field, confidence, extractor, normalizer, or provenance.
- **Approval:** `approved`
- **Verification level:** `unit`
- **Invariants:** `value` is non-empty and is the canonical normalised value for its type. Equality is structural over the two fields. The same entity extracted from two sources produces equal refs. Extractor and normalizer identity/version live on `ExtractedEntity`, not on `EntityRef`.
- **Errors and edge cases:** Empty `value` raises `ValidationError`. Unknown `EntityType` raises `ValidationError`. A raw value with surrounding whitespace is not a valid normalised value unless the type’s canonicaliser produces it.

| Given / Input | When | Expected result |
|---|---|---|
| `EntityRef(URL, "https://example.com")` twice | Compare | Equal |
| `EntityRef(URL, "https://example.com")` and `EntityRef(URL, "https://example.com/")` | Compare | Not equal unless the URL canonicaliser maps them to the same value |
| `EntityRef(PHONE, "+919999999999")` | Validate | Accepted |
| `EntityRef(URL, "")` | Validate | `ValidationError` |

### `TRU-013` Extracted entity records the occurrence

- **Behavior:** `ExtractedEntity` carries the entity type, normalised value, raw extracted value, source field, source span when applicable, extractor name and version, normalizer name and version, and confidence in `[0, 1]`. It is not an identity key.
- **Approval:** `approved`
- **Verification level:** `unit`
- **Invariants:** `normalized_value` equals the `EntityRef.value` for the same occurrence and is produced by the named normalizer/version. For `source_field=text` and `ocr`, `span` is present and `extracted_value` equals the named source text from `start` to `end`. For `qr` and `image`, `span` may be null and the source field identifies the payload. `span.end >= span.start`. Confidence is within bounds. Extractor and normalizer names and versions are non-empty.
- **Errors and edge cases:** Confidence outside `[0, 1]` raises `ValidationError`. A negative span or end before start raises `ValidationError`. A missing extractor, normalizer, or version raises `ValidationError`. A text/OCR occurrence without a span raises `ValidationError`.

| Given / Input | When | Expected result |
|---|---|---|
| Raw `" +91 99999 99999 "` at text span `(0, 18)` | Extract | `type=PHONE`, `normalized_value="+919999999999"`, `extracted_value=" +91 99999 99999 "`, extractor `phone-regex/1`, normalizer `phone/1`, confidence `0.9` |
| Raw `"₹500"` at text span `(10, 14)` | Extract | `type=AMOUNT`, `normalized_value="500"`, `extracted_value="₹500"`, `source_field=text`, span `(10, 14)` |
| OCR text with span `(3, 9)` | Extract | `source_field=ocr`, span `(3, 9)` |
| QR payload without a span | Extract | Accepted; `source_field=qr`, span null |
| Text entity without a span | Validate | `ValidationError` |
| Confidence `1.5` | Validate | `ValidationError` |
| Span `(14, 10)` | Validate | `ValidationError` |
| Missing normalizer version | Validate | `ValidationError` |

### `TRU-014` Artifact identity is content-addressed

- **Behavior:** `ArtifactRef` is exactly the 64-character lowercase hexadecimal SHA-256 digest of the exact bytes. Two refs are equal if and only if their digests are equal. File paths, URLs, blob references, and modality are not part of identity.
- **Approval:** `approved`
- **Verification level:** `unit`
- **Invariants:** The digest is exactly 64 lowercase hexadecimal characters. Input in uppercase is normalised to lowercase. The same bytes under different modalities are the same artifact. `Modality` is carried on the artifact record as interpretation metadata.
- **Errors and edge cases:** A digest of the wrong length or with non-hex characters raises `ValidationError`. A missing digest for a user artifact raises `MissingArtifactRef`. `Modality.UNKNOWN` is allowed as metadata but cannot be used for routing.

| Given / Input | When | Expected result |
|---|---|---|
| APK bytes with SHA-256 `"a" * 64` | Construct | Accepted |
| Same digest with `UNKNOWN` modality | Compare | Equal; modality is metadata |
| Uppercase `"A" * 64` | Construct | Normalised to `"a" * 64` |
| 63-character digest | Construct | `ValidationError` |
| `"g" * 64` | Construct | `ValidationError` |

### `TRU-015` Artifact-derived records retain source identity

- **Behavior:** A record derived from user bytes includes the `artifact_sha256` and the `sandbox_id` that read them. If the record covers only part of the artifact, it includes a `content_locator`; `None` means the whole artifact. The raw bytes are not copied into the ledger record. `sandbox_id` is minted by the orchestrator or task envelope, not chosen by the sandbox.
- **Approval:** `approved`
- **Verification level:** `unit`, `integration`
- **Invariants:** Every derived record traces to an `ArtifactRef`. Ledger payloads contain no `bytes` or attacker-controlled binary blobs. A truncated observation has a locator. A sandbox cannot self-assert its identity.
- **Errors and edge cases:** A derived record with no artifact reference raises `MissingArtifactRef`. A truncated record without a locator raises `MissingContentLocator`. A payload containing `bytes` raises `ValidationError`.

| Given / Input | When | Expected result |
|---|---|---|
| OCR observation from an image artifact | Write | `artifact_sha256`, `sandbox_id`, and no image bytes |
| Text extraction from a message | Write | `artifact_sha256`, parent message artifact, no raw bytes |
| Truncated observation without a locator | Validate | `MissingContentLocator` |
| Derived record with an artifact hash but no parent | Append | `MissingParent` |
| Payload containing `bytes` | Validate | `ValidationError` |

### `TRU-017` Trust and identity contracts serialise without loss

- **Behavior:** `TrustLevel`, `Origin`, `Provenance`, `ReadSet`, `EntityRef`, `ExtractedEntity`, `TextSpan`, `ArtifactRef`, `ParameterPolicy`, and `LabeledValue` serialise to JSON and round-trip to an equal value. Enums use stable lowercase wire strings. Hashes are stored lowercase.
- **Approval:** `approved`
- **Verification level:** `unit`
- **Invariants:** For every valid instance, `model_validate_json(model_dump_json(x)) == x`. `TrustLevel.TRUSTED` serialises as `"trusted"`; `Origin.USER_ARTIFACT` as `"user_artifact"`. `Provenance.label_policy_version` and `Provenance.read_set_id` round-trip exactly. `ExtractedEntity.normalizer_name` and `normalizer_version` round-trip exactly.
- **Errors and edge cases:** Unknown enum strings raise `ValidationError`. Unknown fields are rejected where the model forbids extras. Mixed-case hashes are normalised on input and stored lowercase.

| Given / Input | When | Expected result |
|---|---|---|
| Valid `Provenance` with policy version and read set | JSON round-trip | Equal |
| Valid `ReadSet` | JSON round-trip | Equal |
| Valid `ExtractedEntity` with normalizer fields | JSON round-trip | Equal |
| Valid `EntityRef` | JSON round-trip | Equal |
| Valid `ArtifactRef` | JSON round-trip | Equal |
| `TrustLevel.TRUSTED` | Serialise | `"trusted"` |
| `"trust_level": "deterministic"` | Deserialise | `ValidationError` |

### `TRU-018` Trust decisions are audited

- **Behavior:** Every allow or deny attempts to write an audit record containing the tool name and version, aggregate input trust, label policy version, decision, and denial reason. Raw arguments, prompts, and untrusted payloads are not logged; only hashes or identifiers are logged. If the primary audit sink fails, the record is written to a durable local fallback buffer and an alert is raised. If both the primary sink and fallback fail, the privileged action is denied, `AUDIT_UNAVAILABLE` is returned, a failure metric is incremented, and a best-effort stderr alert is emitted.
- **Approval:** `approved`
- **Verification level:** `unit`, `integration`
- **Invariants:** An audit failure never converts a deny into an allow. A privileged action never executes without an audit record in the primary sink or fallback. The only path with no audit-sink record is total audit unavailability, which is surfaced to the caller and the alert path. The audit record includes enough information to reconstruct the trust decision without raw content.
- **Errors and edge cases:** Audit failure with a working fallback returns the decision and raises an alert. Total audit failure returns `DENY` with `AUDIT_UNAVAILABLE`; no side effect occurs. A successful call records `input_trust=TRUSTED` or `UNTRUSTED`. A denial records the specific reason.

| Given / Input | When | Expected result |
|---|---|---|
| Untrusted control argument denied | Authorize | Primary audit record: `input_trust=UNTRUSTED`, `reason=untrusted_input` |
| Trusted call permitted | Authorize | Primary audit record: `input_trust=TRUSTED` |
| Primary audit sink unavailable | Authorize | Decision recorded in durable fallback; alert raised |
| Primary and fallback unavailable | Authorize a privileged call | `DENY`, `AUDIT_UNAVAILABLE`; metric and stderr alert; no side effect |
| Raw untrusted argument | Audit | Only a hash/identifier is stored |

### `TRU-019` Trust and provenance persist across processes

- **Behavior:** Appending a record and reading it back preserves `trust`, `label_policy_version`, `read_set_id`, and every provenance field exactly. A missing stored trust value reads as `UNTRUSTED` and is flagged for repair, never as `TRUSTED`. Replaying the same input event either deduplicates or produces an identical trust/provenance tuple, using the stored label policy version.
- **Approval:** `approved`
- **Verification level:** `integration`
- **Invariants:** Trust is not recomputed on read. A policy-version change does not silently relabel historical records. Replay is idempotent with respect to trust and provenance. The label policy version is stored with the record in `Provenance.label_policy_version`.
- **Errors and edge cases:** A record written under a different label policy version surfaces a version marker; it is not relabelled. A duplicate replay returns the existing record or an identical tuple. A storage default of `TRUSTED` is a schema error.

| Given / Input | When | Expected result |
|---|---|---|
| Append a record, then read it | Round-trip | `trust`, `label_policy_version`, `read_set_id`, and provenance equal |
| Stored row has no trust column value | Read | `UNTRUSTED`, flagged for repair |
| Same event processed twice | Replay | One record, or identical trust/provenance |
| Historical record under policy v1 | Read under policy v2 | Version surfaced; no silent relabel |
| Schema declares `trust default TRUSTED` | Inspect | Test failure |

### `TRU-020` Constrained fetch of untrusted destinations

- **Behavior:** A tool with `constrained_fetch=True` is the only tool permitted to accept an untrusted control URL. It enforces all of the following: only `http` and `https`; reject userinfo and any port other than 80 or 443; resolve the hostname and reject private, loopback, link-local, multicast, unspecified, reserved, documentation, benchmarking, and cloud-metadata addresses, including `169.254.169.254`; connect to the validated resolved IP rather than a fresh resolution; re-check the peer address at connection time and reject a mismatch (DNS rebinding); revalidate every redirect, allow at most five, and reject scheme downgrade or any blocked destination; use `GET` or `HEAD` only; send no request body, credentials, cookies, or `Authorization` header; enforce connect/read timeouts and a maximum response size; record the destination decision in the audit log.
- **Approval:** `approved`
- **Verification level:** `unit`, `integration`
- **Invariants:** The URL remains `UNTRUSTED`; no trust label changes. No constrained fetch reaches a private, loopback, link-local, reserved, or metadata address, including through a redirect or a re-resolved DNS answer. A blocked fetch dispatches no HTTP request.
- **Errors and edge cases:** A blocked destination raises `FetchBlocked` with a specific reason: `scheme_not_allowed`, `userinfo_not_allowed`, `port_not_allowed`, `private_address`, `metadata_endpoint`, `redirect_limit`, `redirect_blocked`, `dns_rebind`, `timeout`, or `response_too_large`.

| Given / Input | When | Expected result |
|---|---|---|
| `https://example.com/page` | Constrained fetch | Allowed; URL remains `UNTRUSTED` |
| `file:///etc/passwd` | Constrained fetch | `FetchBlocked`, `scheme_not_allowed` |
| `http://127.0.0.1/` | Constrained fetch | `FetchBlocked`, `private_address` |
| `http://169.254.169.254/latest/meta-data` | Constrained fetch | `FetchBlocked`, `metadata_endpoint` |
| Public URL redirecting to `http://10.0.0.1/` | Constrained fetch | `FetchBlocked`, `redirect_blocked` |
| DNS answer changes between validation and connection | Constrained fetch | `FetchBlocked`, `dns_rebind` |
| More than five redirects | Constrained fetch | `FetchBlocked`, `redirect_limit` |
