from typing import Any
from uuid import UUID

from langgraph.graph.state import CompiledStateGraph
from vouch.agents.models import InvestigationState
from vouch.docling import parse_attachments
from vouch.downloader import download_media
from vouch.ingestion import IncomingMessage
from vouch.logger import get_logger
from vouch.preprocessing.adapter import adapt
from vouch.preprocessing.models import CanonicalMessage
from vouch.preprocessing.preprocess import extract_entities_using_llm, preprocess
from vouch.utils import retry, uuid7

logger = get_logger(__file__)

@retry(max_attempts=3)
async def investigate(graph: CompiledStateGraph, msg: CanonicalMessage):
    initial_state: InvestigationState = {
        "investigation_id": uuid7(),
        "tasks": [],
        "hypotheses": [],
        "verdict": None,
        "synthesis": None,
        "exhausted": None,
        "inconclusive": False,
        "input_msg": msg.text_forms.confusable_skeleton,
        "iterations": 0,
        "stage": "orchestration"
    }

    # persist in db with investigation_id & status

    return graph.invoke(
        initial_state,
        config={
            "recursion_limit": 30
        }
    )

async def run(graph: CompiledStateGraph, channel: str, submission_id: UUID, investigation_id: UUID, payload: Any):
    # adapt to IncomingMessage
    msg: IncomingMessage = adapt(channel, str(submission_id), str(investigation_id), payload)

    if len(msg.media) > 0:
        logger.exception("Media cannot be handled yet!")
        raise

    # preprocessing
    canonical_msg: CanonicalMessage = preprocess(msg)

    # - download media
    await download_media(msg, canonical_msg)

    text = parse_attachments([
        media for media in msg.media
        if media.file_path is not None
    ])

    # - llm-based extraction
    await extract_entities_using_llm(text, canonical_msg)

    try:
        # agentic reasoning
        # parallel fan-out
        # fan-in: single ledger writer
        result = await investigate(graph, canonical_msg)
        return result

    except Exception:
        raise
