from langgraph.types import Send
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from vouch.agents.llm import LLMClient
from vouch.agents.models import AgentRole, EvidenceCandidateBatch, InvestigationState, Status, Task, WorkerResult
from vouch.agents.registry import PROFILES, TOOLSETS
from vouch.agents.utils import load_prompt
from vouch.context import WorkerContext
from vouch.db.models import Artifacts, Entities, Ledger, ToolCalls
from vouch.ledger.ledger import AppendResult
from vouch.ledger.models import Evidence, EvidenceDraft
from vouch.logger import get_logger

from langgraph.graph import StateGraph, START, END
from langchain.agents import create_agent
from vouch.preprocessing.entities import normalize_entity
from vouch.security import EntityRef, Provenance, TrustLevel

"""
Anything that the model MUST do -> a tool
Anything that MUST happen -> node code
"""

logger = get_logger(__file__)


def build_agent(ctx: WorkerContext, role: AgentRole):
    """Build a LangChain agent for the given role."""
    try:
        tools = []
        for tool in TOOLSETS[role]:
            t = ctx.tool_registry.resolve(tool)
            if t is not None:
                tools.append(t.handler)

        profile = PROFILES[role]

    except ValueError:
        logger.exception(f"Invalid model profile: {role}")
        raise

    system_prompt = load_prompt(profile.system_prompt_path)
    client = LLMClient(profile, system_prompt=system_prompt)

    return create_agent(
        model=client.base_model,
        tools=tools,
        name=role.value,
        system_prompt=system_prompt,
    )


def build_graph(ctx: WorkerContext):
    """Build and compile the LangGraph investigation workflow."""
    agents = {
        role: build_agent(ctx, role)
        for role in AgentRole
    }

    def delegate_tasks(state: InvestigationState):
        tasks = state["tasks"]

        if not tasks:
            # Decide where an empty task batch should go
            ...

        return [
            Send("execute_task", {
                "investigation_id": state["investigation_id"],
                "task": task,
                "input_message": state["input_msg"]
            })
            for task in tasks
        ]

    async def materialize_drafts(investigation_id: str, candidates: EvidenceCandidateBatch, task: Task):
        drafts = []
        refs: dict[str, set] = {}
        entities = set()

        for candidate in candidates.candidates:
            # deduplicate source ids for eliminating redundant reads
            for ref in candidate.source_refs:
                try:
                    refs[ref.source_type].add(ref.source_id)
                except ValueError:
                    continue

            # (entity_type, observed_value)
            for entity in candidate.entity_candidates:
                normalized_value = normalize_entity(entity.entity_type, entity.observed_value)
                entities.add((entity.entity_type, normalized_value))

        rows = [{
            "entity_type": entity_type,
            "normalized_value": normalized_value
        } for entity_type, normalized_value in entities]

        valid_refs: dict[str, list[str]] = {}
        # batch read ids from EvidenceLedger, ToolCalls, Artifacts
        async with ctx.session_factory() as session:
            if refs["artifact"]:
                stmt = select(Artifacts.artifact_id).where(
                    Artifacts.artifact_id.in_(refs["artifact"])
                )

                result = await session.scalars(stmt)

                for source_id in result.all():
                    valid_refs["artifact"].append(str(source_id))

            if refs["tool_result"]:
                stmt = select(ToolCalls.tool_invocation_id).where(
                    ToolCalls.tool_invocation_id.in_(refs["tool_result"])
                )

                result = await session.scalars(stmt)

                for source_id in result.all():
                    valid_refs["tool_result"].append(str(source_id))

            if refs["evidence"]:
                stmt = select(Ledger.evidence_id).where(
                    Ledger.evidence_id.in_(refs["evidence"])
                )

                result = await session.scalars(stmt)

                for source_id in result.all():
                    valid_refs["evidence"].append(str(source_id))

            stmt = insert(Entities).values(rows).on_conflict_do_nothing()
            await session.execute(stmt)

            stmt = select(Entities.entity_id, Entities.entity_type, Entities.normalized_value).where(
                Entities.entity_id.in_(entities)
            )

            result = await session.scalars(stmt)

            entities = []
            for entity in result.all():
                entities.append(str(entity))

            await session.flush()
            await session.commit()

        for candidate in candidates.candidates:
            sr = [
                r for r in candidate.source_refs
                if r in valid_refs.get(r.source_type, []) != []
            ]

            existing = [
                (e.entity_type, normalize_entity(e.entity_type, e.observed_value))
                for e in candidate.entity_candidates
            ]

            en = [
                EntityRef(type=type, normalized_value=normalized_value)
                for type, normalized_value in entities
                if (type, normalized_value) in existing
            ]

            drafts.append(EvidenceDraft(
            investigation_id=investigation_id,
            kind=candidate.kind,
            finding=candidate.finding,
            source_refs=sr,
            entity_refs=en,
            trust = TrustLevel.UNTRUSTED_ARTIFACT,
            provenance = Provenance(...)
            ))

        return drafts

    def validate_and_accept(drafts: list[EvidenceDraft]) -> list[Evidence]:
        ...

    async def execute_task(state: InvestigationState):
        task = state["task"]

        # Select corresponding agent
        agent = agents[task.assigned_to]

        # Invoke the agent
        result = await agent.ainvoke({
            "messages": [{
                "role": "user", "content": task.model_dump_json(indent=2)
            }]
        })

        batch: EvidenceCandidateBatch = result["structured_output"] # EvidenceCandidateBatch
        drafts: list[EvidenceDraft] = await materialize_drafts(state["investigation_id"], batch, task)

        worker_result = WorkerResult(
            task_id=task.task_id,
            agent= task.assigned_to,
            status=Status.COMPLETED,
            candidate_count=len(batch.candidates),
            errors=[]
        )

        return {
            "drafts": drafts,
            "worker_results": [worker_result]
        }

    async def ledger_writer(state: InvestigationState):
        try:
            drafts = state["drafts"]

            accepted: list[Evidence] = validate_and_accept(drafts)
            append_result: AppendResult = ctx.ledger.append_many(accepted)

        except Exception as e:
            logger.exception(f"Error writing to ledger: {state['investigation_id']}\nError: {str(e)}")
            raise

        return {

        }

    def hypothesis(state: InvestigationState):
        result = agents[AgentRole.HYPOTHESIS_AGENT].invoke({
            "messages": [{
                "role": "user",
                "content": (

                )
            }]
        })

        return {

        }

    def verifier(state: InvestigationState):
        result = agents[AgentRole.VERIFIER].invoke({
            "messages": [{
                "role": "user",
                "content": (

                )
            }]
        })

        return {

        }

    def synthesis(state: InvestigationState):
        result = agents[AgentRole.SYNTHESIS].invoke({
            "messages": [{
                "role": "user",
                "content": (

                )
            }]
        })

        return {

        }

    def orchestrator(state: InvestigationState):
        # 1. Build request from state
        # 2. Invoke the orchestrator agent
        # 3. Validate the propose task-set
        # 4. Enforce task limits and policy

        # 5. Return task batch and update history
        return {}

    builder = StateGraph(InvestigationState)

    builder.add_node("orchestrator", orchestrator)
    builder.add_node("delegate_tasks", delegate_tasks)
    builder.add_node("execute_task", execute_task)
    builder.add_node("ledger_writer", ledger_writer)
    builder.add_node("hypothesis_agent", hypothesis)
    builder.add_node("verifier", verifier)
    builder.add_node("synthesis", synthesis)

    builder.add_edge(START, "orchestrator")
    builder.add_edge("orchestrator", "delegate_tasks")
    builder.add_edge("orchestrator", "verifier")
    builder.add_edge("delegate_tasks", "execute_task")
    builder.add_edge("execute_task", "ledger_writer")
    builder.add_edge("ledger_writer", "verifier")
    builder.add_edge("verifier", "synthesis")
    builder.add_edge("synthesis", END)

    return builder.compile()
