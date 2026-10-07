from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    postgres_url: str = ""
    redis_url: str = ""

    whatsapp_access_token: str = ""
    meta_graph_api_version: str = ""
    telegram_bot_token: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
