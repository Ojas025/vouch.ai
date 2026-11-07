from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    postgres_url: str = ""
    redis_url: str = ""

    whatsapp_access_token: str = ""
    meta_graph_api_version: str = ""
    telegram_bot_token: str = ""
    whatsapp_secret: str = ""
    telegram_webhook_secret: str = ""

    abuse_ch_auth_key: str = ""

    virustotal_api_key: str = ""
    urlscan_api_key: str = ""   # urlscan free tier works unauthenticated

    # Job queue
    job_queue_stream_key: str = "vouch:jobs"
    job_queue_group_name: str = "vouch-workers"
    events_stream: str = "events:stream"

    # Worker
    worker_count: int = Field(default=4, alias="VOUCH_WORKER_COUNT")
    recovery_interval: int = 30

    # Logging
    log_level: str = Field(default="INFO", alias="VOUCH_LOG_LEVEL")
    log_dir: str = Field(default="logs", alias="VOUCH_LOG_DIR")

    # Telemetry
    otlp_protocol: str = Field(default="", alias="OTEL_EXPORTER_OTLP_PROTOCOL")
    otel_service_name: str = "vouch.ai"
    pyroscope_server_address: str = "http://localhost:4040"

    obscura_path: Path | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    def set(self, key: str, value) -> None:
        setattr(self, key, value)


settings = Settings()
