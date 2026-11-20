from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4
import datetime

from langgraph.types import Send
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from vouch.agents.llm import LLMClient
from vouch.agents.middlewares.tool_call import ToolCallMiddleware
from vouch.agents.models import AgentRole, EvidenceCandidateBatch, Hypotheses, InvestigationState, Status, Synthesis, Task, TaskExecutionInput, TaskPlan, VerifierAssessment, WorkerResult
from vouch.agents.registry import PROFILES, TOOLSETS
from vouch.agents.utils import load_prompt
from vouch.context import WorkerContext
from vouch.db.models import Artifacts, Entities, Ledger, ToolCalls
from vouch.decision import DecisionRecord, DecisionStatus, Verdict
from vouch.ledger.models import Evidence, EvidenceDraft
from vouch.logger import get_logger

from langgraph.graph import StateGraph, START, END
from langchain.agents import create_agent
from langchain.agents.middleware import ToolRetryMiddleware
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from vouch.preprocessing.entities import normalize_entity
from vouch.security import EntityRef, Provenance, TrustLevel
from vouch.tools.exceptions import RetryableToolError

"""
Anything that the model MUST do -> a tool
Anything that MUST happen -> node code
"""

logger = get_logger(__file__)

@dataclass
class AgentRuntimeContext:
    investigation_id: UUID
    agent_role: AgentRole
    task_id: UUID | None = None

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

    system_prompt_path = Path(__file__).parent / "prompts" / f"{role.value}.prompt.md"
    system_prompt = load_prompt(system_prompt_path)
    client = LLMClient(profile.llm_profile, system_prompt=system_prompt)

    return create_agent(
        model=client.base_model,
        tools=tools,
        name=role.value,
        system_prompt=system_prompt,
        response_format=profile.llm_profile.output_model,
        middleware=[
            ToolRetryMiddleware(
                max_retries=2,
                retry_on=(RetryableToolError,),
                backoff_factor=2.0,
                initial_delay=1.0,
                max_delay=10.0,
                jitter=True,
                on_failure="continue"
            ),
            ToolCallMiddleware(gateway=ctx.tool_gateway),
        ],
        context_schema=AgentRuntimeContext
    )

async def materialize_drafts(ctx, investigation_id: UUID, candidates: EvidenceCandidateBatch, task: Task):
    drafts: list[EvidenceDraft] = []
    refs: dict[str, set[str]] = {}
    entity_pairs: set[tuple] = set()

    for candidate in candidates.candidates:
        for ref in candidate.source_refs:
            refs.setdefault(ref.source_type, set()).add(ref.source_id)

        for entity in candidate.entity_candidates:
            normalized_value = normalize_entity(entity.entity_type, entity.observed_value)
            entity_pairs.add((entity.entity_type, normalized_value))

    rows = [{
        "entity_type": entity_type,
        "normalized_value": normalized_value
    } for entity_type, normalized_value in entity_pairs]

    valid_refs: dict[str, set[str]] = {}
    id_column = {
        "artifact": Artifacts.artifact_id,
        "tool_result": ToolCalls.tool_invocation_id,
        "evidence": Ledger.evidence_id,
    }

    async with ctx.session_factory() as session:
        if rows:
            stmt = insert(Entities).values(rows).on_conflict_do_nothing()
            await session.execute(stmt)

        for source_type, source_ids in refs.items():
            column = id_column[source_type]
            result = await session.scalars(
                select(column).where(column.in_(source_ids))
            )
            valid_refs[source_type] = {str(source_id) for source_id in result.all()}

        await session.commit()

    for candidate in candidates.candidates:
        sr = [
            r for r in candidate.source_refs
            if r.source_id in valid_refs.get(r.source_type, set())
        ]

        existing = {
            (e.entity_type, normalize_entity(e.entity_type, e.observed_value))
            for e in candidate.entity_candidates
        }

        en = [
            EntityRef(type=entity_type, normalized_value=normalized_value)
            for entity_type, normalized_value in entity_pairs
            if (entity_type, normalized_value) in existing
        ]

        drafts.append(EvidenceDraft(
            investigation_id=investigation_id,
            kind=candidate.kind,
            finding=candidate.finding,
            source_refs=sr,
            entity_refs=en,
            trust=TrustLevel.UNTRUSTED_ARTIFACT,
            provenance=Provenance(
                producer_type="AGENT",
                producer_id=task.assigned_to.value,
                tool_name=task.assigned_to.value,
                tool_version="1.0",
                model_id=PROFILES[task.assigned_to].llm_profile.model,
                observed_at=datetime.datetime.now(datetime.timezone.utc),
            ),
        ))

    return drafts

def validate_and_accept_drafts(drafts: list[EvidenceDraft]) -> list[Evidence]:
    """
    Validate and accept a batch of EvidenceDrafts, returning a list of accepted Evidence.
    """
    accepted: list[Evidence] = []

    for draft in drafts:
        # Validate the draft
        if not draft.source_refs:
            logger.warning(f"Draft {draft} has no source references. Skipping.")
            continue

        if not draft.finding.strip():
            logger.warning(f"Draft {draft} has an empty finding. Skipping.")
            continue

        # Additional validation logic can be added here (e.g., schema checks, trust level checks)

        # Accept the draft as Evidence
        evidence = Evidence(
            evidence_id=str(uuid4()),
            ledger_seq=0,  # This will be set by the ledger when appended
            investigation_id=draft.investigation_id,
            kind=draft.kind,
            finding=draft.finding,
            source_refs=draft.source_refs,
            entity_refs=draft.entity_refs,
            trust=draft.trust,
            provenance=draft.provenance
        )

        accepted.append(evidence)

    return accepted

def validate_tasks(tasks: list[Task]) -> bool:
    """
    Validate the proposed task set from the orchestrator agent.
    Ensure that tasks are well-formed, within budget, and do not conflict with each other.
    """

    # Check for duplicate task IDs
    task_ids = [task.task_id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        logger.warning("Duplicate task IDs found in proposed tasks.")
        return False

    # Check for valid assigned_to roles
    for task in tasks:
        if task.assigned_to not in AgentRole:
            logger.warning(f"Invalid assigned_to role: {task.assigned_to}")
            return False

    # Additional validation logic can be added here (e.g., budget checks, recursion limits)

    return True

async def build_graph(ctx: WorkerContext):
    """
    Build and compile the LangGraph investigation workflow

    Agent responsibilities:
        - Orchestrator: Plan the investigation, assign tasks to agents
        - Task Executor: Execute tasks, generate evidence candidates
        - Ledger Writer: Validate and commit evidence to the ledger
        - Hypothesis Agent: Generate hypotheses based on evidence
        - Verifier: Verify evidence and hypotheses, adversarially challenge claims, point out gaps
        - Synthesis: Synthesize findings, generate final report

    Invariants:
        - An agent cannot expand it's own investigation scope.
        - Untrusted model output cannot directly become accepted evidence.
        - Every piece of evidence must have a source and provenance.
        - Parallel agents must not have conflicting responsibilities, or interfere with each other's state.
        - Repeated execution must not accidentally create duplicate evidence or claims.
        - Every investigation loop must respect resource budgets.
        - A hypotheses must be distinguished from a claim, and a claim must be distinguished from a fact.
        - An inconclusive investigation must be a legitimate outcome, not a failure of the system.
        - Model-generated decisions must pass through application-enforced constraints.
    """

    agents = {
        role: build_agent(ctx, role)
        for role in AgentRole
    }

    async def execute_task(state: TaskExecutionInput):
        task: Task = state["task"]

        # Select corresponding agent
        agent = agents[task.assigned_to]

        # Invoke the agent
        result = await agent.ainvoke(
            {
                "messages": [{
                    "role": "user", "content": task.model_dump_json(indent=2)
                }]
            },
            context=AgentRuntimeContext(
                investigation_id=state["investigation_id"],
                agent_role=task.assigned_to,
                task_id=task.task_id,
            ),
        )

        batch: EvidenceCandidateBatch = result["structured_response"] # EvidenceCandidateBatch
        drafts: list[EvidenceDraft] = await materialize_drafts(ctx, state["investigation_id"], batch, task)

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

            accepted: list[Evidence] = validate_and_accept_drafts(drafts)
            _ = ctx.ledger.append_many(accepted)

        except Exception as e:
            logger.exception(f"Error writing to ledger: {state['investigation_id']}\nError: {str(e)}")
            raise

        return {
            "drafts": [],
            "accepted_evidences": [e.evidence_id for e in accepted],

        }

    def hypothesis(state: InvestigationState):
        result = agents[AgentRole.HYPOTHESIS_AGENT].invoke(
            {
                "messages": [{
                    "role": "user",
                    "content": "Generate hypotheses based on the following evidence: {}".format(
                        state["accepted_evidences"],
                    )
                }]
            },
            context=AgentRuntimeContext(
                investigation_id=state["investigation_id"],
                agent_role=AgentRole.HYPOTHESIS_AGENT,
            ),
        )

        hypotheses: list[Hypotheses] = result["structured_response"]["hypotheses"]

        return {
            "hypotheses": hypotheses
        }

    def verifier(state: InvestigationState):
        result = agents[AgentRole.VERIFIER].invoke(
            {
                "messages": [{
                    "role": "user",
                    "content": "Verify the following evidence: {} and hypotheses: {}\n\nOriginal request: {}".format(
                        state["accepted_evidences"],
                        state["hypotheses"],
                        state["original_request"]
                    )
                }]
            },
            context=AgentRuntimeContext(
                investigation_id=state["investigation_id"],
                agent_role=AgentRole.VERIFIER,
            ),
        )

        assessments: list[VerifierAssessment] = result["structured_response"]["verifier_assessments"]
        verdict: Verdict | None = result["structured_response"]["verdict"]

        return {
            "verdict": verdict,
            "hypotheses": [],
            "accepted_evidences": [],
            "verifier_assessments": assessments
        }

    def synthesis(state: InvestigationState):
        result = agents[AgentRole.SYNTHESIS].invoke(
            {
                "messages": [{
                    "role": "user",
                    "content": "Based on the following verdict, it's supporting and refuting claims, and the original request, synthesize a final report: {}".format(
                        state["verdict"],
                    )
                }]
            },
            context=AgentRuntimeContext(
                investigation_id=state["investigation_id"],
                agent_role=AgentRole.SYNTHESIS,
            ),
        )

        response = result["structured_response"]["response"]

        # persist the synthesis to the ledger or database if needed

        return {
            "synthesis": Synthesis(
                decision=DecisionRecord(
                    decision_id=uuid4(),
                    investigation_id=state["investigation_id"],
                    status=DecisionStatus("decided") if result["structured_response"]["decision"] != "inconclusive" else DecisionStatus("insufficient_evidence"),
                    verdicts=[state["verdict"]] if state["verdict"] else [],
                    resolved_hypotheses_ids=[],
                    unresolved_hypotheses_ids=[],
                    contradictions=[],
                    evidence_confidence="medium",  # Placeholder, should be computed
                    evidence_freshness="mixed",  # Placeholder, should be computed
                    decided_at=datetime.datetime.utcnow()
                ),
                response=response
            )
        }

    def orchestrator(state: InvestigationState):
        if state["iterations"] == 0:
            msg = state["original_request"]
        else:
            msg = "Original request: {}\n\nCurrent findings: {}".format(
                state["original_request"],
                state["verifier_assessments"]
            )

        # Invoke the orchestrator agent
        result = agents[AgentRole.ORCHESTRATOR].invoke(
            {
                "messages": [{
                    "role": "user",
                    "content": (msg)
                }]
            },
            context=AgentRuntimeContext(
                investigation_id=state["investigation_id"],
                agent_role=AgentRole.ORCHESTRATOR,
            ),
        )

        tasks = result["structured_response"]["tasks"]

        # Validate the propose task-set
        # Add reason for any rejected or empty taskset: budget, recursion, timeout, etc.
        if not tasks:
            logger.warning(
                "Orchestrator produced no tasks for investigation",
                extra={
                    "investigation_id": state["investigation_id"],
                    "reason": result["structured_response"]["disposition"]
                }
            )

            return {
                "task_plan": TaskPlan(
                    investigation_id=state["investigation_id"],
                    tasks=[],
                    disposition=result["structured_response"]["disposition"]
                ),
                "iterations": state["iterations"] + 1
            }

        if not validate_tasks(tasks):
            logger.warning(f"Orchestrator produced invalid tasks for investigation {state['investigation_id']}")
            return {
                "task_plan": TaskPlan(
                    investigation_id=state["investigation_id"],
                    tasks=[],
                    disposition="malformed_tasks"
                ),
                "iterations": state["iterations"] + 1
            }

        # Enforce task limits and policy

        # Return task batch and update history
        return {
            "task_plan": TaskPlan(
                investigation_id=state["investigation_id"],
                tasks=tasks,
                disposition=result["structured_response"]["disposition"]
            ),
            "iterations": state["iterations"] + 1
        }

    def delegate_tasks(state: InvestigationState):
        plan = state["task_plan"]

        if not plan or not plan.tasks:
            logger.warning(f"No tasks to delegate for investigation {state['investigation_id']}")
            return []

        return [
            Send("execute_task", {
                "investigation_id": state["investigation_id"],
                "task": task,
                "input_message": state["original_request"]
            })
            for task in plan.tasks
        ]

    def task_validation_routing(state: InvestigationState):
        plan = state["task_plan"]

        if not plan:
            return "no_actionable_tasks"

        if not plan.tasks:
            return "no_actionable_tasks"

        if plan.disposition == "dispatch":
            return "delegate_tasks"
        else:
            return plan.disposition

    try:
        # Use a connection pool for checkpointer as multiple concurrent investigations may be running and writing to the same Postgres database.
        checkpointer = AsyncPostgresSaver(ctx.db_pool)
        await checkpointer.setup()

        builder = StateGraph(InvestigationState)

        builder.add_node("orchestrator", orchestrator)
        builder.add_node("delegate_tasks", delegate_tasks)
        builder.add_node("execute_task", execute_task)
        builder.add_node("ledger_writer", ledger_writer)
        builder.add_node("hypotheses_agent", hypothesis)
        builder.add_node("verifier", verifier)
        builder.add_node("synthesis", synthesis)

        builder.add_edge(START, "orchestrator")
        builder.add_conditional_edges(
            "orchestrator",
            task_validation_routing,
            {
                "delegate_tasks": "delegate_tasks",
                "no_actionable_tasks": "verifier",
                "blocked": "verifier",
                "malformed_tasks": "verifier"
            })
        builder.add_edge("delegate_tasks", "execute_task")
        builder.add_edge("execute_task", "ledger_writer")
        builder.add_edge("ledger_writer", "hypotheses_agent")
        builder.add_edge("hypotheses_agent", "verifier")
        builder.add_conditional_edges(
            "verifier",
            lambda state: "synthesis" if state["verdict"] is not None else "continue",
            {
                "synthesis": "synthesis",
                "continue": "orchestrator"
            }
        )
        builder.add_edge("synthesis", END)

        return builder.compile(checkpointer=checkpointer)

    except Exception as e:
        logger.exception(f"Error building graph: {str(e)}")
        raise
