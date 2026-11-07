from pathlib import Path

from vouch.agents.models import AgentRole, Capability, LLMProfile
from vouch.tools.models import ToolDefinition, ToolSideEffects
from vouch.tools.tools.entity import EntityIntelArgs, EntityIntelOutput, entity_intel
from vouch.tools.tools.web import ExtractPageContentArgs, ExtractPageContentOutput, ScreenshotPageArgs, ScreenshotPageOutput, WebInspectArgs, WebInspectOutput, extract_page_content, screenshot_page, web_inspect
from vouch.tools.tools.artifact import artifact_extract, ArtifactExtractArgs, ArtifactExtractOutput

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

SKILLS_DIR = Path(__file__).parent / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

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
]

PROFILES: dict[AgentRole, LLMProfile] = {
    AgentRole.ORCHESTRATOR: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA, Capability.REASONING],
        system_prompt_path=PROMPTS_DIR / "orchestrator.prompt.md",
    ),
    AgentRole.APK_AGENT: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA],
        system_prompt_path=PROMPTS_DIR / "apk_agent.prompt.md",
    ),
    AgentRole.WEB_AGENT: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA],
        system_prompt_path=PROMPTS_DIR / "WEB_AGENT.prompt.md",
    ),
    AgentRole.ARTIFACT_AGENT: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA],
        system_prompt_path=PROMPTS_DIR / "artifact_agent.prompt.md",
    ),
    AgentRole.HYPOTHESIS_AGENT: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA, Capability.VISION],
        system_prompt_path=PROMPTS_DIR / "hypotheses_agent.prompt.md"
    ),
    AgentRole.VERIFIER: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA],
        system_prompt_path=PROMPTS_DIR / "verifier.prompt.md",
    ),
    AgentRole.SYNTHESIS: LLMProfile(
        model="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=[Capability.TOOLS, Capability.JSON_SCHEMA],
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
    AgentRole.WEB_AGENT: {
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
