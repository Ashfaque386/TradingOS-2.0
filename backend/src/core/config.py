from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "development"

    database_url: str = "postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
