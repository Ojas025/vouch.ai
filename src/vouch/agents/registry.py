from pathlib import Path

from vouch.agents.models import AgentRole, LLMProfile

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

SKILLS_DIR = Path(__file__).parent / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

PROFILES: dict[AgentRole, LLMProfile] = {
    AgentRole.ORCHESTRATOR: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=["tools", "streaming", "json_schema"],
        system_prompt_path=PROMPTS_DIR / "orchestrator.prompt.md",
    ),
    AgentRole.HYPOTHESIS_AGENT: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=["tools", "streaming", "json_schema", "vision"],
        system_prompt_path=PROMPTS_DIR / "hypotheses_agent.prompt.md"
    ),
    AgentRole.VERIFIER: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=["tools", "streaming", "json_schema"],
        system_prompt_path=PROMPTS_DIR / "verifier.prompt.md",
    ),
    AgentRole.SYNTHESIS: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=["json_schema", "streaming"],
        system_prompt_path=PROMPTS_DIR / "synthesis.prompt.md"
    )
}

TOOLSETS = {
    AgentRole.ORCHESTRATOR: {
        "evidence_query",
        "evidence_trace",
        "memory_recall",
        "decide"
    },
    AgentRole.URL_AGENT: {
        "evidence_query",
        "evidence_trace",
        "entity_intel",
        "url_trace",
        "web_search",
        "web_fetch"
    },
    AgentRole.APK_AGENT: {
        "evidence_query",
        "evidence_trace",
        "entity_intel",
        "apk_profile",
        "apk_code",
        "apk_flow"
    },
    AgentRole.ARTIFACT_AGENT: {
        "evidence_query",
        "evidence_trace",
        "artifact_extract",
        "entity_intel",
    },
    AgentRole.HYPOTHESIS_AGENT: {
        "evidence_query",
        "evidence_trace",
        "entity_intel",
        "memory_recall"
    },
    AgentRole.VERIFIER: {
        "evidence_query",
        "evidence_trace",
        "entity_intel",
        "memory_recall"
    },
    AgentRole.SYNTHESIS: {}
}

def resolve_tools(agent: AgentRole, registry: dict):
    allowed_tools = TOOLSETS[agent]
    return [
        registry[name]
        for name in allowed_tools
    ]
