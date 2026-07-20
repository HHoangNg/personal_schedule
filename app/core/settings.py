from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    llm_provider: str = "mock"
    llm_model: str = "gpt-4o-mini"
    openai_model: str = "gpt-4o-mini"
    gemini_model: str = "gemini-2.5-flash"
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    database_path: str = "data/productivity.db"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "productivity_memory"
    qdrant_vector_size: int = 768
    qdrant_vector_name: str = "dense"
    qdrant_sync_enabled: bool = True
    voyage_api_key: str | None = None
    voyage_model: str = "voyage-3"
    gmail_credentials_path: str = "secrets/gmail_credentials.json"
    gmail_token_path: str = "data/gmail_token.json"
    gmail_scan_days: int = 3
    gmail_max_results: int = 50
    google_calendar_credentials_path: str = "secrets/google_calendar_credentials.json"
    google_calendar_token_path: str = "data/google_calendar_token.json"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
