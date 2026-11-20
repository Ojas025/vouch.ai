from pathlib import Path

from vouch.agents.llm import LLMProfile
from vouch.agents.models import AgentProfile, AgentRole, Capability, EvidenceCandidateBatch, HypothesesOutput, OrchestratorOutput, SynthesisOutput, VerifierOutput
from vouch.tools.models import ToolDefinition, ToolSideEffects
from vouch.tools.tools.entity import EntityIntelArgs, EntityIntelOutput, entity_intel
from vouch.tools.tools.web import ExtractPageContentArgs, ExtractPageContentOutput, ScreenshotPageArgs, ScreenshotPageOutput, WebInspectArgs, WebInspectOutput, WebSearchArgs, extract_page_content, screenshot_page, web_inspect, web_search

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

SKILLS_DIR = Path(__file__).parent / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

PROMPT_VERSION = "v1"

TOOL_DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        tool_name="entity_intel",
        description="Single typed entity lookup...",
        handler=entity_intel,
        args_model=EntityIntelArgs,
        output_model=EntityIntelOutput,
        side_effects=ToolSideEffects.NETWORK,
        timeout_sec=30.0,
    ),
    ToolDefinition(
        tool_name="web_inspect",
        description="Inspect a URL and follow HTTP redirects to record the hop chain and landing status.",
        handler=web_inspect,
        args_model=WebInspectArgs,
        output_model=WebInspectOutput,
        side_effects=ToolSideEffects.NETWORK,
        timeout_sec=15.0,
    ),
    ToolDefinition(
        tool_name="extract_page_content",
        description="Execute JavaScript and extract rendered Markdown text, titles, and hyperlinks from a webpage via Obscura.",
        handler=extract_page_content,
        args_model=ExtractPageContentArgs,
        output_model=ExtractPageContentOutput,
        side_effects=ToolSideEffects.NETWORK,
        timeout_sec=25.0,
    ),
    ToolDefinition(
        tool_name="screenshot_page",
        description="Render a webpage in a headless browser and capture a 1280x720 PNG screenshot artifact.",
        handler=screenshot_page,
        args_model=ScreenshotPageArgs,
        output_model=ScreenshotPageOutput,
        side_effects=ToolSideEffects.NETWORK,
        timeout_sec=25.0,
    ),
    ToolDefinition(
        tool_name="web_search",
        description="Search on the web to query about a claim.",
        handler=web_search,
        args_model=WebSearchArgs,
        output_model=WebInspectOutput,
        side_effects=ToolSideEffects.NETWORK,
        timeout_sec=25.0
    )
]

TOOLSETS = {
    AgentRole.ORCHESTRATOR: {
        "evidence_query",
        "evidence_trace",
        "memory_recall",
        "decide"
    },
    AgentRole.WEB_AGENT: {
        "evidence_query",
        "evidence_trace",
        "entity_intel",
        "web_inspect",
        "extract_page_content",
        "screenshot_page",
        "web_search"
    },
    AgentRole.APK_AGENT: {
        "evidence_query",
        "evidence_trace",
        "entity_intel"
    },
    AgentRole.ARTIFACT_AGENT: {
        "evidence_query",
        "evidence_trace",
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

PROFILES: dict[AgentRole, AgentProfile] = {
    AgentRole.ORCHESTRATOR: AgentProfile(
        role=AgentRole.ORCHESTRATOR,
        llm_profile=LLMProfile(
            model="minicpm",
            api_key="not-required",
            base_url="http://127.0.0.1:8080/v1",
            capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA, Capability.REASONING],
            output_model=OrchestratorOutput,
        ),
        allowed_tools=frozenset(TOOLSETS[AgentRole.ORCHESTRATOR]),
        network_policy="none",
        prompt_version=PROMPT_VERSION,
    ),
    AgentRole.APK_AGENT: AgentProfile(
        role=AgentRole.APK_AGENT,
        llm_profile=LLMProfile(
            model="minicpm",
            api_key="not-required",
            base_url="http://127.0.0.1:8080/v1",
            capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA],
            output_model=EvidenceCandidateBatch,
        ),
        allowed_tools=frozenset(TOOLSETS[AgentRole.APK_AGENT]),
        network_policy="search_only",
        prompt_version=PROMPT_VERSION,
    ),
    AgentRole.WEB_AGENT: AgentProfile(
        role=AgentRole.WEB_AGENT,
        llm_profile=LLMProfile(
            model="minicpm",
            api_key="not-required",
            base_url="http://127.0.0.1:8080/v1",
            capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA],
            output_model=EvidenceCandidateBatch,
        ),
        allowed_tools=frozenset(TOOLSETS[AgentRole.WEB_AGENT]),
        network_policy="search_only",
        prompt_version=PROMPT_VERSION,
    ),
    AgentRole.ARTIFACT_AGENT: AgentProfile(
        role=AgentRole.ARTIFACT_AGENT,
        llm_profile=LLMProfile(
            model="minicpm",
            api_key="not-required",
            base_url="http://127.0.0.1:8080/v1",
            capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA],
            output_model=EvidenceCandidateBatch,
        ),
        allowed_tools=frozenset(TOOLSETS[AgentRole.ARTIFACT_AGENT]),
        network_policy="search_only",
        prompt_version=PROMPT_VERSION,
    ),
    AgentRole.HYPOTHESIS_AGENT: AgentProfile(
        role=AgentRole.HYPOTHESIS_AGENT,
        llm_profile=LLMProfile(
            model="minicpm",
            api_key="not-required",
            base_url="http://127.0.0.1:8080/v1",
            capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA, Capability.VISION],
            output_model=HypothesesOutput,
        ),
        allowed_tools=frozenset(TOOLSETS[AgentRole.HYPOTHESIS_AGENT]),
        network_policy="search_only",
        prompt_version=PROMPT_VERSION,
    ),
    AgentRole.VERIFIER: AgentProfile(
        role=AgentRole.VERIFIER,
        llm_profile=LLMProfile(
            model="minicpm",
            api_key="not-required",
            base_url="http://127.0.0.1:8080/v1",
            capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA],
            output_model=VerifierOutput,
        ),
        allowed_tools=frozenset(TOOLSETS[AgentRole.VERIFIER]),
        network_policy="search_only",
        prompt_version=PROMPT_VERSION,
    ),
    AgentRole.SYNTHESIS: AgentProfile(
        role=AgentRole.SYNTHESIS,
        llm_profile=LLMProfile(
            model="minicpm",
            api_key="not-required",
            base_url="http://127.0.0.1:8080/v1",
            capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA],
            output_model=SynthesisOutput,
        ),
        allowed_tools=frozenset(TOOLSETS[AgentRole.SYNTHESIS]),
        network_policy="none",
        prompt_version=PROMPT_VERSION,
    )
}
