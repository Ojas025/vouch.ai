import asyncio

from vouch.agents.agents import graph
from vouch.agents.models import InvestigationState
from vouch.agents.registry import PROFILES
from vouch.docling import parse_attachments
from vouch.ledger.ledger import Ledger
from vouch.ledger.models import EvidenceDraft
from vouch.downloader import download_media, route
from vouch.decision import DecisionRecord
from vouch.ingestion import Event, IncomingMessage
from vouch.logger import get_logger
from vouch.preprocessing.adapter import adapt
from vouch.preprocessing.preprocess import extract_entities_using_llm, preprocess
from vouch.tools.gateway import ToolGateway
from vouch.tools.rate_limit import RateLimiter
from vouch.tools.registry import ToolRegistry
from vouch.utils import uuid7

logger = get_logger(__file__)

def investigate():
    initial_state: InvestigationState = {
        "investigation_id": str(uuid7()),
        "tasks": [],
        "hypotheses": [],
        "verdict": None,
        "synthesis": None,
        "exhausted": None,
        "inconclusive": False,
    }

    return graph.invoke(
        initial_state,
        config={
            "recursion_limit": 30
        }
    )

async def run(event: Event):
    # adapt to IncomingMessage
    msg: IncomingMessage = adapt(event)

    if len(msg.media) > 0:
        logger.exception("Media cannot be handled yet!")
        raise

    # preprocessing
    canonical_msg = preprocess(msg)

    # - download media
    await download_media(msg, canonical_msg)

    text = parse_attachments([
        media for media in msg.media
        if media.file_path is not None
    ])

    # - llm-based extraction
    await extract_entities_using_llm(text, canonical_msg)

    # agentic reasoning
    # parallel fan-out
    # fan-in: single ledger writer
    decision = investigate()

    # persist decision
    persist_decision(decision)

    send_message(decision)
