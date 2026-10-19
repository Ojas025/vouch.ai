from typing import Callable

from vouch.agents.models import LLMProfile


PROFILES: dict[str, LLMProfile] = {
    "orchestrator": LLMProfile(
        name="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=["tools", "streaming", "json_schema"],
    ),
    "hypotheses_agent": LLMProfile(
        name="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=["tools", "streaming", "json_schema", "vision"],
    ),
    "verifier": LLMProfile(
        name="minicpm",
        api_key="not-required",
        base_url="http://127.0.0.1:8080/v1",
        capabilities=["tools", "streaming", "json_schema"],
    )
}

TOOLSETS: dict[str, Any] = {}
