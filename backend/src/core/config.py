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

    # Strategy sandbox (src/engine/sandbox/, Build Spec §9) AND, as of
    # Phase 10, the real DuckDB + Parquet data lake root (src/data/lake.py)
    # -- the same directory, on purpose: a sandboxed strategy's read-only
    # data mount now genuinely sees real ingested market data instead of an
    # empty directory. Relative to CWD, same convention as
    # agent_gateway_config_path above.
    data_lake_path: str = "data_lake"
    # Phase 10: nightly backup destination (src/data/backup.py) -- must be a
    # different directory than data_lake_path so a backup is a real, separate
    # copy rather than a no-op self-copy.
    data_lake_backup_path: str = "data_lake_backups"
    # Phase 10 (src/data/nse_calendar.py): movable/lunisolar NSE holiday
    # reference file. Relative to CWD, same convention as
    # agent_gateway_config_path -- local `uvicorn` dev from backend/ needs
    # this overridden to "../config/nse_holidays.json" same as that setting.
    nse_holidays_path: str = "config/nse_holidays.json"
    sandbox_warm_pool_size: int = 2
    sandbox_default_timeout_seconds: int = 30
    sandbox_cpu_seconds: int = 10
    sandbox_memory_bytes: int = 512 * 1024 * 1024

    # Temporal (src/engine/optimization/temporal_workflows.py, Build Spec
    # §10 Monte Carlo distribution). Defaults to localhost for local
    # `uvicorn` dev, same convention as database_url/redis_url above;
    # docker-compose.yml overrides this to the `temporal` service's
    # in-network hostname for the container.
    temporal_address: str = "localhost:7233"

    # Phase 8 (Build Spec §3, §13, §20): encrypted local broker-credentials
    # store -- Vault is intentionally dropped for 2.0's self-hosted,
    # single-operator scale (see the Build Spec's Technology Stack table).
    # secrets_encryption_key must be a urlsafe-base64 32-byte Fernet key
    # (cryptography.fernet.Fernet.generate_key()); left unset by default
    # so a missing key fails loudly the first time the store is actually
    # used (src.security.secrets_store.get_secrets_store), not silently
    # at import time. Relative to CWD, same convention as
    # agent_gateway_config_path/data_lake_path above.
    secrets_store_path: str = "secrets/broker_credentials.enc"
    secrets_encryption_key: str | None = None

    # Phase 11 (Build Spec §19): WORM-style local append-only audit
    # archive (src/audit/archive.py) -- see that module's docstring for
    # why this is a local file store rather than MinIO. Relative to CWD,
    # same convention as data_lake_path above.
    audit_archive_path: str = "audit_archive"
    # Order-dispatch latency budget (tick -> broker handoff), Build Spec
    # §16's "measured against a documented budget" -- the spec names the
    # requirement but not a number; 500ms is this build's own documented
    # choice (an HTTP round-trip to a broker API, not a co-located
    # exchange connection, so sub-100ms isn't realistic) and is what
    # src.observability.metrics compares every real dispatch against.
    order_dispatch_latency_budget_ms: float = 500.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
