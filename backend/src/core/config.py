from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "development"

    database_url: str = "postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    cors_origins: list[str] = ["http://localhost:3000"]

    # Relative to CWD by default, which is correct for the single container
    # (WORKDIR /app, config/ bind-mounted at /app/config — see
    # docker-compose.yml). Local dev running `uvicorn` from backend/ needs
    # this overridden in .env to "../config/tradingos.config.json".
    agent_gateway_config_path: str = "config/tradingos.config.json"

    # LLM router (src/agents/llm_router.py) provider credentials. All
    # optional: a provider with no key configured simply fails fast and the
    # router falls through to the next one in infra.llm_providers.order.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    deepseek_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"


@lru_cache
def get_settings() -> Settings:
    return Settings()
