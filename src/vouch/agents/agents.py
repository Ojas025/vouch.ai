from vouch.agents.llm import LLMClient
from vouch.agents.models import InvestigationState, RuntimeDeps
from vouch.agents.registry import PROFILES, TOOLSETS
from vouch.logger import get_logger

import frontmatter
from pathlib import Path
from langgraph.graph import StateGraph, START, END
from langchain_core.prompts import ChatPromptTemplate

logger = get_logger(__file__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

SKILLS_DIR = Path(__file__).parent / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

def create_model(profile_name: str):
    try:
        profile = PROFILES[profile_name]
        tools = TOOLSETS[profile_name]
        if not profile or not tools:
            raise ValueError

    except ValueError:
        logger.exception(f"Invalid model profile: {profile_name}")
        raise

    llm_client = LLMClient(profile)
    return llm_client.bind_tools(tools)

def load_prompt(path: Path):
    document = frontmatter.load(path)

    metadata = document.metadata
    template = ChatPromptTemplate.from_messages([
        ("system", document.content)
    ])

    return metadata, template

orchestrator_llm = create_model("orchestrator")
hypotheses_llm = create_model("hypotheses")
verifier_llm = create_model("verifier")
synthesis_llm = create_model("synthesis")

def orchestrator(state: InvestigationState):
    system_prompt = load_prompt(PROMPTS_DIR / "orchestrator.prompt.md")

def hypothesis_agent(state: InvestigationState):
    system_prompt = load_prompt(PROMPTS_DIR / "hypotheses_agent.prompt.md")

def verifier(state: InvestigationState):
    system_prompt = load_prompt(PROMPTS_DIR / "verifier.prompt.md")

def synthesis(state: InvestigationState):
    system_prompt = load_prompt(PROMPTS_DIR / "synthesis.prompt.md")

graph = StateGraph(InvestigationState, context_schema=RuntimeDeps)

graph.add_node("orchestrator", orchestrator)
graph.add_node("hypothesis_agent", hypothesis_agent)
graph.add_node("verifier", verifier)

graph.add_edge(START, "orchestrator")
graph.add_edge("orchestrator", "hypothesis_agent")
graph.add_edge("hypothesis_agent", "verifier")
graph.add_edge("verifier", "synthesis")
graph.add_edge("synthesis", END)

graph = graph.compile()

graph.invoke()
