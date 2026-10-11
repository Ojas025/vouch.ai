from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    postgres_url: str = ""
    redis_url: str = ""

    whatsapp_access_token: str = ""
    meta_graph_api_version: str = ""
    telegram_bot_token: str = ""

    abuse_ch_auth_key: str = ""

    # Fact Services source profile:
    #   "core"     - URLhaus, urlscan, RDAP
    #   "extended" - adds ThreatFox and OpenPhish
    fact_profile: Literal["core", "extended"] = "core"

    # Optional sources. When blank, the source degrades to no facts rather than erroring.
    virustotal_api_key: str = ""
    urlscan_api_key: str = ""   # urlscan free tier works unauthenticated

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
