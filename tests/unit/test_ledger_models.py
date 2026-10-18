"""Unit tests for the evidence ledger and decision domain models.

These are pure pydantic models, so the contract under test is validation
(discriminated union dispatch, literals, bounds) and serialization round-trips
— the shapes the ledger writer and the gate rely on.
"""

from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from vouch.decision import (
    Contradiction,
    DecisionRecord,
    DecisionStatus,
    EvidenceCoverage,
    RiskLevel,
    Verdict,
)
from vouch.agents.models import Claim, ClaimType, ThreatClass
from vouch.ingestion import AttachmentMetadata
from vouch.ledger.models import (
    Evidence,
    EvidenceDraft,
    EvidenceKind,
    EvidencePayload,
    InvestigationSnapshot,
    ObservationPayload,
    SimilarityMatchPayload,
    SourceFactPayload,
)
from vouch.preprocessing.models import EntityType, Modality
from vouch.security import EntityRef, Provenance, TrustLevel


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

PROVENANCE = Provenance(
    producer_type="SERVICE",
    producer_id="fact-service",
    tool_name="urlhaus",
    tool_version="1.0.0",
    artifact_sha256="a" * 64,
    observed_at=NOW,
)

PAYLOAD_ADAPTER = TypeAdapter(EvidencePayload)


# --------------------------------------------------------------------------
# EvidencePayload discriminated union
# --------------------------------------------------------------------------


class TestSourceFactPayload:
    @pytest.mark.parametrize(
        "source",
        ["URLHAUS", "THREATFOX", "OPENPHISH", "RDAP", "IFSC", "URLSCAN",
         "VIRUSTOTAL", "ALLOWLIST"],
    )
    def test_every_declared_source_is_accepted(self, source):
        payload = SourceFactPayload(
            source=source, classification="malicious", source_confidence=0.9
        )

        assert payload.source == source
        assert payload.kind is EvidenceKind.SOURCE_FACT

    def test_unknown_source_is_rejected(self):
        with pytest.raises(ValidationError):
            SourceFactPayload(source="MADEUP", classification="x")

    def test_optional_fields_have_defaults(self):
        payload = SourceFactPayload(source="RDAP", classification="registered")

        assert payload.source_confidence is None
        assert payload.detail == {}

    def test_discriminator_dispatches_to_source_fact(self):
        validated = PAYLOAD_ADAPTER.validate_python(
            {"kind": "source_fact", "source": "URLHAUS", "classification": "phish"}
        )

        assert isinstance(validated, SourceFactPayload)

    def test_unknown_discriminator_is_rejected(self):
        with pytest.raises(ValidationError):
            PAYLOAD_ADAPTER.validate_python({"kind": "not_a_kind"})


class TestObservationPayload:
    @pytest.mark.parametrize(
        "modality",
        [Modality.TEXT, Modality.URL, Modality.APK, Modality.DOCUMENT,
         Modality.IMAGE, Modality.AUDIO],
    )
    def test_accepted_across_modalities(self, modality):
        payload = ObservationPayload(
            modality=modality, key="title", value="Verify your account"
        )

        assert payload.modality is modality
        assert payload.kind is EvidenceKind.OBSERVATION
        assert payload.truncated is False

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("text", "text"), (7, 7), (1.5, 1.5), (True, True), (["a", "b"], ["a", "b"])],
    )
    def test_value_accepts_scalar_and_list_of_string(self, value, expected):
        payload = ObservationPayload(
            modality=Modality.TEXT, key="k", value=value
        )

        assert payload.value == expected

    def test_dict_value_is_rejected(self):
        with pytest.raises(ValidationError):
            ObservationPayload(modality=Modality.TEXT, key="k", value={"a": 1})

    def test_discriminator_dispatches_to_observation(self):
        validated = PAYLOAD_ADAPTER.validate_python(
            {"kind": "observation", "modality": "text", "key": "body", "value": "hi"}
        )

        assert isinstance(validated, ObservationPayload)


class TestSimilarityMatchPayload:
    def test_valid_match_is_accepted(self):
        payload = SimilarityMatchPayload(
            method="minhash",
            other_entity=AttachmentMetadata(kind=Modality.IMAGE, file_id="f-1"),
            score=0.87,
            verified=True,
        )

        assert payload.kind is EvidenceKind.SIMILARITY_MATCH
        assert payload.method == "minhash"
        assert payload.score == 0.87

    @pytest.mark.parametrize(
        "method", ["minhash", "icon_phash", "cert_fingerprint", "entity_graph"]
    )
    def test_every_similarity_method_is_accepted(self, method):
        payload = SimilarityMatchPayload(
            method=method,
            other_entity=AttachmentMetadata(),
            score=0.5,
            verified=False,
        )

        assert payload.method == method

    def test_unknown_method_is_rejected(self):
        with pytest.raises(ValidationError):
            SimilarityMatchPayload(
                method="cosine",
                other_entity=AttachmentMetadata(),
                score=0.5,
                verified=False,
            )

    def test_discriminator_dispatches_to_similarity_match(self):
        validated = PAYLOAD_ADAPTER.validate_python(
            {
                "kind": "similarity_match",
                "method": "icon_phash",
                "other_entity": {},
                "score": 0.4,
                "verified": False,
            }
        )

        assert isinstance(validated, SimilarityMatchPayload)


# --------------------------------------------------------------------------
# EvidenceDraft / Evidence
# --------------------------------------------------------------------------


class TestEvidenceDraft:
    def test_created_with_provenance_and_trust(self):
        draft = EvidenceDraft(
            investigation_id="inv-1",
            status="ok",
            modality=Modality.URL,
            subject=None,
            payload=SourceFactPayload(source="URLHAUS", classification="malicious"),
            trust=TrustLevel.EXTERNAL_SOURCE,
            provenance=PROVENANCE,
        )

        assert draft.trust is TrustLevel.EXTERNAL_SOURCE
        assert draft.provenance.producer_id == "fact-service"
        assert draft.new_entities == []

    @pytest.mark.parametrize(
        "status", ["ok", "partial", "timeout", "sandbox_violation", "parse_error"]
    )
    def test_status_literals(self, status):
        draft = EvidenceDraft(
            investigation_id="inv-1",
            status=status,
            modality=Modality.TEXT,
            subject=None,
            payload=ObservationPayload(modality=Modality.TEXT, key="k", value="v"),
            trust=TrustLevel.DETERMINISTIC,
            provenance=PROVENANCE,
        )

        assert draft.status == status

    def test_unknown_status_is_rejected(self):
        with pytest.raises(ValidationError):
            EvidenceDraft(
                investigation_id="inv-1",
                status="fine",
                modality=Modality.TEXT,
                subject=None,
                payload=ObservationPayload(modality=Modality.TEXT, key="k", value="v"),
                trust=TrustLevel.DETERMINISTIC,
                provenance=PROVENANCE,
            )

    def test_untrusted_artifact_trust_is_allowed(self):
        draft = EvidenceDraft(
            investigation_id="inv-1",
            status="ok",
            modality=Modality.IMAGE,
            subject=None,
            payload=ObservationPayload(modality=Modality.IMAGE, key="ocr", value="₹500"),
            trust=TrustLevel.UNTRUSTED_ARTIFACT,
            provenance=PROVENANCE,
        )

        assert draft.trust is TrustLevel.UNTRUSTED_ARTIFACT


class TestEvidence:
    def test_append_only_record_creation(self):
        record = Evidence(
            evidence_id="ev-1",
            ledger_seq=7,
            investigation_id="inv-1",
            subject=EntityRef(type=EntityType.URL, value="https://example.com"),
            payload=SourceFactPayload(source="URLHAUS", classification="malicious"),
            trust=TrustLevel.EXTERNAL_SOURCE,
            provenance=PROVENANCE,
        )

        assert record.ledger_seq == 7
        assert record.supersedes is None
        assert record.subject.value == "https://example.com"

    def test_serialization_round_trip(self):
        record = Evidence(
            evidence_id="ev-1",
            ledger_seq=1,
            investigation_id="inv-1",
            subject=None,
            payload=SimilarityMatchPayload(
                method="minhash",
                other_entity=AttachmentMetadata(),
                score=0.5,
                verified=False,
            ),
            trust=TrustLevel.DETERMINISTIC,
            provenance=PROVENANCE,
            supersedes="ev-0",
        )

        restored = Evidence.model_validate_json(record.model_dump_json())

        assert restored == record
        assert restored.payload.kind is EvidenceKind.SIMILARITY_MATCH

    def test_payload_keeps_its_discriminator_through_serialization(self):
        draft = EvidenceDraft(
            investigation_id="inv-1",
            status="partial",
            modality=Modality.APK,
            subject=None,
            payload=SourceFactPayload(source="VIRUSTOTAL", classification="trojan"),
            trust=TrustLevel.EXTERNAL_SOURCE,
            provenance=PROVENANCE,
        )

        data = draft.model_dump(mode="json")
        assert data["payload"]["kind"] == "source_fact"

        restored = EvidenceDraft.model_validate(data)
        assert isinstance(restored.payload, SourceFactPayload)


class TestInvestigationSnapshot:
    def test_snapshot_is_constructible_and_serializable(self):
        snapshot = InvestigationSnapshot(
            snapshot_id="snap-1",
            investigation_id="inv-1",
            ledger_seq_high=12,
            evidence_ids=["ev-1", "ev-2"],
            entities=[EntityRef(type=EntityType.UPI_VPA, value="merchant@okaxis")],
            created_at=NOW,
            policy_version="v1",
        )

        restored = InvestigationSnapshot.model_validate_json(snapshot.model_dump_json())
        assert restored.evidence_ids == ["ev-1", "ev-2"]
        assert restored.ledger_seq_high == 12


# --------------------------------------------------------------------------
# Decision models
# --------------------------------------------------------------------------


def make_verdict(**overrides):
    fields = dict(
        threat_class=ThreatClass.PHISHING,
        level=RiskLevel.CONFIRMED,
        supporting_claim_ids=["c-1", "c-2"],
        refuting_claim_ids=["c-3"],
        rule_id="rule-phish-1",
    )
    fields.update(overrides)
    return Verdict(**fields)


class TestVerdict:
    def test_serialization_round_trip(self):
        verdict = make_verdict()

        restored = Verdict.model_validate_json(verdict.model_dump_json())

        assert restored == verdict
        assert restored.threat_class is ThreatClass.PHISHING
        assert restored.level is RiskLevel.CONFIRMED

    @pytest.mark.parametrize(
        ("level", "value"),
        [(RiskLevel.NONE, 0), (RiskLevel.WEAK, 1),
         (RiskLevel.SUSPECTED, 2), (RiskLevel.CONFIRMED, 3)],
    )
    def test_risk_levels_are_ordered_ints(self, level, value):
        assert level.value == value

    def test_refuting_claims_default_to_empty(self):
        verdict = Verdict(
            threat_class=ThreatClass.PHISHING,
            level=RiskLevel.WEAK,
            supporting_claim_ids=["c-1"],
            rule_id="rule-phish-1",
        )

        assert verdict.refuting_claim_ids == []

    def test_unknown_threat_class_is_rejected(self):
        with pytest.raises(ValidationError):
            make_verdict(threat_class="scam")


class TestContradictionAndCoverage:
    def test_contradiction_kinds_are_closed(self):
        contradiction = Contradiction(
            claim_a="c-1", claim_b="c-2", critical=True,
            kind="direct_negation",
        )

        assert contradiction.kind == "direct_negation"
        with pytest.raises(ValidationError):
            Contradiction(claim_a="c-1", claim_b="c-2", critical=False, kind="vibes")

    def test_coverage_reports_unresolved_scopes(self):
        coverage = EvidenceCoverage(
            required_scopes=["url", "ifsc"],
            resolved_scopes=["url"],
            unresolved_scopes=["ifsc"],
        )

        assert coverage.unresolved_scopes == ["ifsc"]
        assert coverage.stale_evidence_ids == []


def make_decision(**overrides):
    fields = dict(
        decision_id="dec-1",
        investigation_id="inv-1",
        snapshot_id="snap-1",
        status=DecisionStatus.DECIDED,
        verdicts=[make_verdict()],
        resolved_claim_ids=["c-1", "c-2"],
        unresolved_claim_ids=["c-3"],
        contradictions=[],
        coverage=EvidenceCoverage(
            required_scopes=["url"], resolved_scopes=["url"], unresolved_scopes=[]
        ),
        evidence_confidence="high",
        evidence_freshness="fresh",
        auditor_invoked=True,
        policy_version="v1",
        scorer_version="s1",
        decided_at=NOW,
    )
    fields.update(overrides)
    return DecisionRecord(**fields)


class TestDecisionRecord:
    @pytest.mark.parametrize(
        "status",
        [DecisionStatus.DECIDED, DecisionStatus.INSUFFICIENT_EVIDENCE,
         DecisionStatus.ANALYSIS_UNAVAILABLE],
    )
    def test_every_status_is_constructible(self, status):
        assert make_decision(status=status).status is status

    def test_abstention_is_a_valid_record_without_verdicts(self):
        # Insufficient evidence: no verdicts, claims left unresolved — the
        # abstention path must be as valid as a decided one.
        record = make_decision(
            status=DecisionStatus.INSUFFICIENT_EVIDENCE,
            verdicts=[],
            resolved_claim_ids=[],
            unresolved_claim_ids=["c-1"],
            auditor_invoked=False,
            evidence_confidence="low",
        )

        assert record.verdicts == []
        assert record.status is DecisionStatus.INSUFFICIENT_EVIDENCE

    def test_serialization_round_trip(self):
        record = make_decision()

        restored = DecisionRecord.model_validate_json(record.model_dump_json())

        assert restored == record
        assert restored.decided_at == NOW

    @pytest.mark.parametrize("confidence", ["low", "medium", "high"])
    def test_confidence_is_a_closed_set(self, confidence):
        assert make_decision(evidence_confidence=confidence).evidence_confidence == confidence

    def test_unknown_confidence_is_rejected(self):
        with pytest.raises(ValidationError):
            make_decision(evidence_confidence="certain")

    def test_unknown_status_string_is_rejected(self):
        with pytest.raises(ValidationError):
            make_decision(status="kinda_bad")


class TestClaim:
    def make_claim(self, **overrides):
        fields = dict(
            claim_id="c-1",
            investigation_id="inv-1",
            snapshot_id="snap-1",
            subject="SUBMISSION",
            object="https://example.com is a phishing page",
            scope="url",
            claim_type=ClaimType.INFERENTIAL,
            evidence_refs=["ev-1"],
            confidence=0.8,
            observed_at=NOW,
            provenance=PROVENANCE,
        )
        fields.update(overrides)
        return Claim(**fields)

    def test_valid_claim_is_constructible(self):
        claim = self.make_claim()

        assert claim.claim_type is ClaimType.INFERENTIAL
        assert claim.trust is TrustLevel.MODEL_OUTPUT
        assert claim.asserted is True

    def test_claim_requires_at_least_one_evidence_ref(self):
        with pytest.raises(ValidationError):
            self.make_claim(evidence_refs=[])

    @pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
    def test_confidence_bounds_are_inclusive(self, confidence):
        assert self.make_claim(confidence=confidence).confidence == confidence

    @pytest.mark.parametrize("confidence", [-0.1, 1.1])
    def test_confidence_outside_unit_interval_is_rejected(self, confidence):
        with pytest.raises(ValidationError):
            self.make_claim(confidence=confidence)

    def test_claim_may_be_deterministic_but_not_untrusted(self):
        claim = self.make_claim(trust=TrustLevel.DETERMINISTIC)
        assert claim.trust is TrustLevel.DETERMINISTIC

        with pytest.raises(ValidationError):
            self.make_claim(trust=TrustLevel.UNTRUSTED_ARTIFACT)

    def test_subject_may_be_an_entity_ref(self):
        claim = self.make_claim(
            subject=EntityRef(type=EntityType.UPI_VPA, value="merchant@okaxis")
        )

        assert claim.subject.value == "merchant@okaxis"

    def test_observed_at_survives_serialization(self):
        restored = Claim.model_validate_json(self.make_claim().model_dump_json())

        assert restored.observed_at == NOW
