import hashlib
from pathlib import Path
import uuid
from fastapi import HTTPException
from pydantic import BaseModel
import requests
import httpx

from vouch.config import settings
from vouch.ingestion import AttachmentMetadata, Channel, IncomingMessage, MediaDownloadMetadata
from vouch.ledger.models import EvidenceDraft
from vouch.logger import get_logger
from vouch.observability.context import bind_correlation
from vouch.observability.spans import ATTR_MODALITY, SPAN_INVESTIGATION_FANOUT, span
from vouch.preprocessing.models import CanonicalMessage, Modality
from vouch.utils import delete_from_path, retry

class BranchJob(BaseModel):
    job_id: str
    investigation_id: str
    modality: Modality
    artifact: AttachmentMetadata | None = None
    attempt_no: int = 1

class RoutePlan(BaseModel):
    """What route() hands to the fan-out stage: one BranchJob per artifact."""
    jobs: list[BranchJob] = []

async def analyze_apk(job: BranchJob) -> EvidenceDraft:
    return await _analyze_apk(job)


async def _analyze_apk(job: BranchJob) -> EvidenceDraft:
    ...

"""
TODOs:
    - Finish telegram media fetch
    - Fix MediaDownloadMetadata schema to consider telegram
    - Make route() return TriagePlan
"""

logger = get_logger(__file__)

MEDIA_SAVE_DIR = Path(__file__).parent / "downloads"
MEDIA_SAVE_DIR.mkdir(parents=True, exist_ok=True)

@retry(max_attempts=3)
async def fetch_media(metadata: MediaDownloadMetadata, file_path: Path) -> bool:
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}"
    }

    try:
        client = httpx.AsyncClient()
        async with client.stream("GET", metadata.url, headers=headers, timeout=60) as response:
            response.raise_for_status()

            hasher = hashlib.sha256()

            with open(file_path, "wb") as file:
                async for chunk in response.aiter_bytes(1024*1024):
                    if chunk:
                        hasher.update(chunk)
                        file.write(chunk)

            if metadata.sha256 is not None:
                actual = hasher.hexdigest()

                if actual != metadata.sha256:
                    logger.error(
                        "artifact integrity verification failed",
                        extra={"event": "media.integrity_failed", "component": "router", "reason_code": "integrity_mismatch"},
                    )
                    delete_from_path(file_path)
                    return False

                return True

    except HTTPException as e:
        logger.error(
            "failed to download media artifact",
            extra={"event": "media.download_failed", "component": "router", "error_category": type(e).__name__},
        )

def get_media_kind(media: AttachmentMetadata) -> Modality:
    is_apk = (
        media.file_name.lower().endswith(".apk")
        or media.mime_type == "application/vnd.android.package-archive"
    )

    if is_apk:
        return Modality.APK

    return Modality.UNKNOWN

@retry(max_attempts=3)
async def _request_telegram_media_download(media_id: str) -> MediaDownloadMetadata | None:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getFile?file_id={media_id}"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30)

        response.raise_for_status()
        data = response.json()

        if response.status_code == 200:
            logger.info(
                "media download metadata received",
                extra={"event": "media.metadata_received", "component": "router", "media_id": media_id},
            )

        return MediaDownloadMetadata.model_validate(data)

    except requests.HTTPError as e:
        logger.error(
            "failed to fetch media download metadata",
            extra={"event": "media.metadata_failed", "component": "router", "error_category": type(e).__name__},
        )

@retry(max_attempts=3)
async def _request_whatsapp_media_download(media_id: str) -> MediaDownloadMetadata | None:
    url = f"https://graph.facebook.com/{settings.meta_graph_api_version}/{media_id}"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=30)

        response.raise_for_status()
        data = response.json()

        if response.status_code == 200:
            logger.info(
                "media download metadata received",
                extra={"event": "media.metadata_received", "component": "router", "media_id": media_id},
            )

        return MediaDownloadMetadata.model_validate(data)

    except requests.HTTPError as e:
        logger.error(
            "failed to fetch media download metadata",
            extra={"event": "media.metadata_failed", "component": "router", "error_category": type(e).__name__},
        )

async def request_media_download(channel: Channel, media_id: str) -> MediaDownloadMetadata | None:
    if channel == Channel.TELEGRAM:
        return await _request_telegram_media_download(media_id)

    return await _request_whatsapp_media_download(media_id)

async def route(msg: IncomingMessage, canonical_msg: CanonicalMessage) -> RoutePlan:
    """Route request based on the modalities present within the forwared message."""

    # APK
    if len(msg.media) == 0:
        # No modalities, route to main text branch later
        return

    try:
        for media in msg.media:
            metadata = await request_media_download(msg.channel, media.file_id)
            if metadata is None:
                logger.info(
                    "media metadata not found",
                    extra={"event": "media.metadata_not_found", "component": "router"},
                )
                continue

            media.download_metadata = metadata

            media.kind = get_media_kind(media)
            file_downloaded = await fetch_media(metadata, MEDIA_SAVE_DIR / media.file_name)

            if not file_downloaded:
                logger.debug(
                    "failed to download file",
                    extra={"event": "media.download_failed", "component": "router", "reason_code": "integrity_or_network"},
                )
                continue

            media.file_path = MEDIA_SAVE_DIR / media.file_name

        # Process each media as per branch
        for media in msg.media:
            if media.kind == Modality.TEXT:
                # main branch
                pass

            investigation_id = "inv_" + f"{msg.channel}:{msg.submission_id}"
            job_id = "job_" + str(uuid.uuid4())

            # The span is not a try/except of its own: the one at the end of
            # this function already terminates and reports the same exception.
            with span(SPAN_INVESTIGATION_FANOUT, {
                "vouch.job.id": job_id,
                ATTR_MODALITY: media.kind.value,
                "vouch.investigation.id": investigation_id,
            }):
                with bind_correlation(investigation_id=investigation_id, job_id=job_id):
                    logger.info(
                        "branch dispatched",
                        extra={
                            "event": "branch.dispatched",
                            "component": "router",
                            "modality": media.kind.value,
                        },
                    )

                    if media.kind == Modality.APK and media.file_path is not None:
                        await analyze_apk(BranchJob(
                            investigation_id=investigation_id,
                            job_id=job_id,
                            modality=Modality.APK,
                            artifact=media,
                            attempt_no=1
                        ))

                    logger.info(
                        "branch completed",
                        extra={
                            "event": "branch.completed",
                            "component": "router",
                            "modality": media.kind.value,
                            "outcome": "ok",
                        },
                    )

    except Exception as e:
        logger.error(
            "routing failed",
            extra={"event": "routing.failed", "component": "router", "error_category": type(e).__name__},
            exc_info=True,
        )
