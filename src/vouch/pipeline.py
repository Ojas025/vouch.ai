from typing import Any
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from vouch.agents.models import InvestigationState
from vouch.docling import parse_attachments
from vouch.downloader import download_media
from vouch.ingestion import IncomingMessage
from vouch.logger import get_logger
from vouch.preprocessing.adapter import adapt
from vouch.preprocessing.models import CanonicalMessage
from vouch.preprocessing.preprocess import extract_entities_using_llm, preprocess
from vouch.utils import retry

logger = get_logger(__file__)

@retry(max_attempts=3)
async def investigate(investigation_id: UUID, graph: CompiledStateGraph, msg: CanonicalMessage):
    snapshot = await graph.aget_state({
        "configurable": {
            "thread_id": str(investigation_id)
        },
        "recursion_limit": 30,
    })

    state: InvestigationState | None = None

    if not snapshot.values:
        logger.info("Initializing new investigation", extra={"event": "investigation.init", "investigation_id": str(investigation_id)})
        state = {
            "investigation_id": investigation_id,
            "task_plan": None,
            "hypotheses": [],
            "accepted_evidences": [],
            "verifier_assessments": [],
            "worker_results": [],
            "drafts": [],
            "verdict": None,
            "synthesis": None,
            "exhausted": None,
            "inconclusive": False,
            "original_request": msg.text_forms.confusable_skeleton,
            "iterations": 0,
            "stage": "orchestration"
        }

    elif snapshot.next:
        logger.info(
            "Resuming investigation from checkpoint",
            extra={
                "event": "investigation.resume",
                "investigation_id": str(investigation_id),
                "checkpoint_state": snapshot.next
            }
        )
        state = None
    else:
        logger.info(
            "investigation already completed",
            extra={
                "event": "investigation.already_done",
                "investigation_id": str(investigation_id),
            }
        )
        return None

    # persist in db with investigation_id & status
    return await graph.ainvoke(
        state,
        config={
            "configurable": {
                "thread_id": str(investigation_id)
            },
            "recursion_limit": 30,
        },
        durability="sync"
    )

async def run(graph: CompiledStateGraph, channel: str, submission_id: UUID, investigation_id: UUID, payload: Any):
    # adapt to IncomingMessage
    msg: IncomingMessage = adapt(channel, str(submission_id), str(investigation_id), payload)

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
        result = await investigate(investigation_id, graph, canonical_msg)

        # Remove temporary execution state
        logger.info(
            "Deleting temporary investigation state",
            extra={
                "event": "investigation.cleanup",
                "investigation_id": str(investigation_id),
            }
        )
        checkpointer = graph.checkpointer
        if not isinstance(checkpointer, AsyncPostgresSaver):
            raise RuntimeError("Checkpointer is not initialized")

        await checkpointer.adelete_thread(str(investigation_id))

        return result

    except Exception:
        raise
