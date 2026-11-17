from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field
from vouch.agents.llm import LLMClient, LLMProfile
from vouch.agents.models import Capability
from vouch.agents.utils import load_prompt
from vouch.agents.registry import PROMPTS_DIR
from vouch.docling import parse_attachments
from vouch.ingestion import AttachmentMetadata
from vouch.logger import get_logger
from vouch.security import Provenance, TrustLevel

logger = get_logger(__file__)

class InspectArtifactArgs(BaseModel):
    artifact_metadata: AttachmentMetadata

class Observation(BaseModel):
    text: str = Field(description="Extracted observation text", min_length=3, max_length=1000)
    source_artifact_id: UUID
    mime_type: str

class ObservationBatch(BaseModel):
    observations: list[str] = Field(description="List of extracted observations", default_factory=list)

class InspectArtifactOutput(BaseModel):
    content: str = ""
    observations: list[Observation] = Field(default_factory=list)
    error: str | None = None
    trust: TrustLevel = TrustLevel.UNTRUSTED_ARTIFACT
    provenance: Provenance | None = None

llm = LLMClient(LLMProfile(
    model="minicpm",
    api_key="not-required",
    base_url="http://127.0.0.1:8080/v1",
    capabilities=[Capability.JSON_SCHEMA, Capability.REASONING],
), output_model=ObservationBatch)  # Initialize the LLM client

async def extract_observations(parsed_output: str, meta: AttachmentMetadata) -> list[Observation]:
    """Extract observations from parsed output."""
    system_prompt = load_prompt(PROMPTS_DIR / "observer.prompt.md")  # Load the observer prompt

    # Use the LLM to extract observations from the parsed output
    response = await llm.ainvoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": parsed_output},
        ],
    )

    return [Observation(
            text=obs,
            source_artifact_id=UUID(meta.file_id),
            mime_type=meta.mime_type
        ) for obs in response.observations]

async def inspect_artifact(args: InspectArtifactArgs):
    """Extract markdown text from an artifact."""
    meta = args.artifact_metadata

    def failure(error: str):
        return InspectArtifactOutput(
            error=error,
            provenance=Provenance(
                producer_type="SERVICE",
                producer_id="vouch",
                tool_name="inspect_artifact",
                tool_version="0.1.0",
                observed_at=datetime.now()
            )
        )

    if not meta.file_path or not Path(meta.file_path).exists():
        return failure(f"File path {meta.file_path} does not exist.")

    if meta.status != "downloaded":
        return failure(f"Artifact status is {meta.status}, expected 'downloaded'.")

    if meta.mime_type in ["application/pdf", "image/png", "image/jpeg", "image/jpg", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]:
        parsed_output = parse_attachments([meta])
    else:
        return failure(f"Unsupported mime type: {meta.mime_type}.")

    observations: list[Observation] = await extract_observations(parsed_output, meta)

    return InspectArtifactOutput(
        content=parsed_output,
        observations=observations,
        provenance=Provenance(
            producer_type="SERVICE",
            producer_id="vouch",
            tool_name="inspect_artifact",
            tool_version="0.1.0",
            observed_at=datetime.now()
        )
    )
