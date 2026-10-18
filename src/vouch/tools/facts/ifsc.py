"""IFSC (Razorpay) — Indian bank IFSC code resolution.

A 404 is a *finding* here, not an error: it means the code matches no live branch,
which is exactly the IFSC_INVALID signal. So this source inspects the status itself
instead of treating it as "no data".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from langchain.tools import tool

from vouch.preprocessing.models import EntityType
from vouch.security import EntityRef
from vouch.tools.facts._shared import FactSource, SourceFact, ThreatClassification, call


class IFSCArgs(BaseModel):
    """Args: the raw IFSC code string; uppercased internally."""
    code: str

    @classmethod
    def from_entity(cls, entity: EntityRef) -> IFSCArgs:
        return cls(code=entity.value)

@tool
async def lookup_ifsc(args: IFSCArgs) -> list[SourceFact]:
    """IFSC code lookup against Razorpay: validates an Indian bank branch code."""
    now = datetime.now(timezone.utc)
    code = args.code.upper()

    try:
        resp = await call("razorpay.com", "GET", f"https://ifsc.razorpay.com/{code}")
    except Exception:
        return []

    if resp.status_code == 404:
        return [SourceFact(
            source=FactSource.IFSC,
            indicator=EntityRef(type=EntityType.IFSC, value=args.code),
            classification=ThreatClassification.IFSC_INVALID,
            observed_at=now,
            source_confidence=1.0,
            expires_at=now + timedelta(days=7),
            detail={"ifsc": code},
        )]

    if resp.status_code != 200:
        return []

    data = resp.json()
    return [SourceFact(
        source=FactSource.IFSC,
        indicator=EntityRef(type=EntityType.IFSC, value=args.code),
        classification=ThreatClassification.IFSC_VALID,
        observed_at=now,
        source_confidence=1.0,
        expires_at=now + timedelta(days=7),
        detail={
            "ifsc": code,
            "bank": data.get("BANK", ""),
            "branch": data.get("BRANCH", ""),
            "state": data.get("STATE", ""),
            "city": data.get("CITY", ""),
        },
    )]
