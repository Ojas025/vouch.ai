"""Fact tools — TTL-cached, deterministic entity intelligence. No LLM.

Each source is its own module exposing a <Tool>Args schema and an async tool that
emits SourceFacts. Shared models, cache, HTTP helpers, and the lookup() service live
in _shared.py.
"""

from vouch.tools.facts._shared import (
    DEFAULT_TTLS,
    REGISTRY,
    FactLookupStatus,
    FactQuery,
    FactServiceResult,
    FactSource,
    SourceFact,
    ThreatClassification,
    build_registry,
    get_facts,
    lookup,
    merged_ttl,
    set_facts,
    to_evidence_drafts,
)
from vouch.tools.facts.ifsc import IFSCArgs, lookup_ifsc
from vouch.tools.facts.openphish import OpenPhishArgs, lookup_openphish
from vouch.tools.facts.rdap import RdapArgs, lookup_rdap
from vouch.tools.facts.threatfox import ThreatFoxArgs, lookup_threatfox
from vouch.tools.facts.urlhaus import UrlhausArgs, lookup_urlhaus
from vouch.tools.facts.urlscan import UrlscanArgs, lookup_urlscan
from vouch.tools.facts.virustotal import VirusTotalArgs, lookup_virustotal

__all__ = [
    "DEFAULT_TTLS",
    "REGISTRY",
    "FactLookupStatus",
    "FactQuery",
    "FactServiceResult",
    "FactSource",
    "SourceFact",
    "ThreatClassification",
    "build_registry",
    "get_facts",
    "lookup",
    "merged_ttl",
    "set_facts",
    "to_evidence_drafts",
    "IFSCArgs",
    "OpenPhishArgs",
    "RdapArgs",
    "ThreatFoxArgs",
    "UrlhausArgs",
    "UrlscanArgs",
    "VirusTotalArgs",
    "lookup_ifsc",
    "lookup_openphish",
    "lookup_rdap",
    "lookup_threatfox",
    "lookup_urlhaus",
    "lookup_urlscan",
    "lookup_virustotal",
]
