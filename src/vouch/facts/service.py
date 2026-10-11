from __future__ import annotations
import asyncio
import time

from vouch.facts import cache
from vouch.facts.models import (
    FactLookupStatus, FactQuery, FactServiceResult, SourceFact,
)
from vouch.facts.sources import REGISTRY
from vouch.security.models import EntityRef


async def _guard(src, entity: EntityRef) -> tuple[list[SourceFact], str]:
    """Run one source, converting any failure into an empty result + error string so one bad
    source can never take down the batch."""
    try:
        return await src(entity), ""
    except Exception as e:
        return [], str(e)


async def lookup(
    entity: EntityRef,
    investigation_id: str,
    redis,
    registry: dict | None = None,
) -> FactServiceResult:
    """Fan out across every source registered for this entity type, in parallel, merge the
    results, and cache them.

    Usage:
        result = await lookup(entity, investigation_id, redis)
    """
    sources = (registry if registry is not None else REGISTRY).get(entity.type, [])
    query = FactQuery(entity=entity, investigation_id=investigation_id)
    t0 = time.monotonic()

    if not sources:
        return FactServiceResult(
            query=query,
            status=FactLookupStatus.SKIPPED,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    cached = await cache.get_facts(redis, entity)
    if cached is not None:
        return FactServiceResult(
            query=query,
            status=FactLookupStatus.CACHED,
            facts=cached,
            cache_hit=True,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    results = await asyncio.gather(
        *(_guard(src, entity) for src in sources), return_exceptions=True
    )

    facts: list[SourceFact] = []
    errors: list[str] = []
    for src, res in zip(sources, results):
        if isinstance(res, BaseException):
            # Belt-and-braces: _guard already catches Exception, but CancelledError is a
            # BaseException and must not abort the batch.
            errors.append(f"{src.__name__}: {res}")
            continue
        src_facts, err = res
        facts.extend(src_facts)
        if err:
            errors.append(f"{src.__name__}: {err}")

    if facts:
        # Single write, TTL bounded by the shortest-lived fact.
        await cache.set_facts(redis, entity, facts, cache.merged_ttl(facts))

    return FactServiceResult(
        query=query,
        status=FactLookupStatus.HIT if facts else FactLookupStatus.MISS,
        facts=facts,
        errors=errors,
        duration_ms=(time.monotonic() - t0) * 1000,
    )


def to_evidence_drafts(result: FactServiceResult) -> list:
    """Convert a lookup result into EvidenceDrafts ready for the ledger writer."""
    from vouch.branches.models import EvidenceDraft, SourceFactPayload
    from vouch.preprocessing.models import Modality
    from vouch.security.models import Provenance, TrustLevel

    drafts = []
    for fact in result.facts:
        if fact.is_expired:
            continue

        drafts.append(EvidenceDraft(
            investigation_id=result.query.investigation_id,
            status="ok",
            # Facts derived from a text message are TEXT-modality regardless of entity type.
            modality=Modality.TEXT,
            subject=None,
            payload=SourceFactPayload(
                source=fact.source.value,
                classification=fact.classification.value,
                source_confidence=fact.source_confidence,
                detail=fact.detail,
            ),
            trust=TrustLevel.EXTERNAL_SOURCE,
            provenance=Provenance(
                producer_type="SERVICE",
                producer_id=fact.source.value,
                tool_name=f"fact_service:{fact.source.value.lower()}",
                tool_version="1.0",
                observed_at=fact.observed_at,
            ),
        ))
    return drafts
