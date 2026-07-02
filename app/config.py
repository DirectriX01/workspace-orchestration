"""Application settings loaded from environment / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Workspace Orchestrator."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/orchestrator"
    sync_database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5433/orchestrator"
    redis_url: str = "redis://localhost:6380/0"
    openai_api_key: str = ""
    llm_provider: str = "openai"  # "openai" | "fake"
    embeddings_provider: str = "openai"  # "openai" | "fake"
    llm_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    mock_google: bool = True
    default_tz: str = "Asia/Kolkata"
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/callback"
    demo_user_email: str = "demo@example.com"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
