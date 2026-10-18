import asyncio
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from vouch.ledger.models import EvidenceDraft
from vouch.router import route
from vouch.decision import DecisionRecord
from vouch.ingestion import Event
from vouch.logger import get_logger
from vouch.preprocessing.adapter import adapt
from vouch.preprocessing.preprocess import preprocess

logger = get_logger(__file__)

async def run(event: Event) -> DecisionRecord | None:
    # adapt to IncomingMessage
    msg = adapt(event)

    if len(msg.media) > 0:
        logger.exception("Media cannot be handled yet!")
        raise

    # run preprocessing + extraction pipeline
    canonical_msg = preprocess(msg)

    # modality-based routing
    plan = route(msg, canonical_msg)

    # parallel fan-out
    results: list[EvidenceDraft] = await asyncio.gather(
        *(execute_branch(job) for job in plan.jobs),
        return_exceptions=True
    )
    drafts = flatten(results, errors_as_drafts=True)

    # fan-in: single ledger writer
    snapshot = ledger.append(investigation_id, drafts)

    skills = get_modality_skills()

    tools = select_required_tools()

    # agentic reasoning
    decision = await investigate(snapshot, skills, tools)

    # persist decision
    update_state(decision)
    return decision
