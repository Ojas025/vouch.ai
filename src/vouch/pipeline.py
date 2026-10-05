from typing import List
import unicodedata
from disarm import Text

from vouch.branches.router import route
from vouch.ingestion.models import Event
from vouch.logger import get_logger
from vouch.preprocessing.adapter import adapt
from vouch.preprocessing.preprocess import preprocess

logger = get_logger(__file__)

async def run(event: Event):
    # adapt to IncomingMessage
    msg = adapt(event)

    if len(msg.media) > 0:
        logger.exception("Media cannot be handled yet!")
        raise

    # run preprocessing + extraction pipeline
    canonical_msg = preprocess(msg)

    route(msg, canonical_msg)
