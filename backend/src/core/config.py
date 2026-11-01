from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "development"

    database_url: str = "postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos"
    redis_url: str = "redis://localhost:6379/0"

    # General, blanket API rate limit (Phase 21, docs/phase20-old-vs-new-
    # comparison.md item 24) -- deliberately coarse and IP-keyed, not
    # per-user (the middleware runs before FastAPI resolves
    # Depends(get_current_user), so no authenticated identity is available
    # yet at the point a request must be let through or rejected). This is
    # a different, blunter layer than the existing identity-aware limits
    # already scoped to specific safety-critical surfaces (live trading,
    # inbound webhooks, notification senders) -- those are unchanged and
    # stay narrower/stricter than this one on purpose.
    api_rate_limit_per_minute: int = 300

    # See src/core/db.py's engine construction for why these are settings
    # rather than hardcoded: the default (20/20, sized for the WebSocket
    # fan-out load measured in the Build Spec §21-22 hardening pass) is
    # right for backend/Dockerfile's own container, which has nothing else
    # competing for memory in it. Dockerfile.allinone's single container
    # also runs Postgres, Redis, the frontend's Node server, and nginx, all
    # sharing one memory budget -- it overrides these much smaller via the
    # allinone-supervisord.conf api program's environment.
    db_pool_size: int = 20
    db_max_overflow: int = 20

    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # A plain (optionally comma-separated) string, not list[str]:
    # pydantic-settings JSON-decodes env values for collection-typed
    # fields before any validator sees them, which would make a plain
    # "http://localhost:3003" from docker-compose.yml a hard startup
    # error instead of a single origin. Docker Compose sets this from
    # FRONTEND_HOST_PORT so it tracks wherever the frontend's dynamic
    # port allocator (scripts/docker-up.js) actually landed, instead of
    # only ever accepting the frontend's default port. Use
    # cors_origins_list (below) to consume it.
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

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
    huggingface_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"

    # Agent long-term vector memory (src/memory/). Both halves are required
    # and either one missing leaves memory honestly "unavailable" -- never a
    # fake: there is no built-in fallback embedder, because a hash or random
    # vector would make similarity search return confident-looking nonsense.
    # `qdrant_url`: the Qdrant server (docker-compose.yml's `qdrant` service
    # sets http://qdrant:6333); empty disables memory. The embedding
    # provider is one of ollama|openai|gemini|custom and reuses that
    # provider's credentials from Settings (the encrypted LLM provider
    # store, then these env-backed fields above), so there is no second
    # place to enter a key. These two are only the fallback: an operator's
    # choice saved in the UI (the `memory_settings` table) wins over them.
    # `memory_embedding_model` has no default on
    # purpose: an embedding model is a real choice (Ollama needs one pulled,
    # e.g. nomic-embed-text), and a dimension mismatch if it later changes is
    # reported, not papered over.
    qdrant_url: str = ""
    qdrant_api_key: str | None = None
    memory_embedding_provider: str = ""
    memory_embedding_model: str = ""
    # How many prior lessons a pipeline run recalls into its prompts.
    memory_recall_limit: int = 3
    # The least cosine similarity a lesson needs to be recalled into a prompt at all. Without
    # a floor recall returns the nearest few lessons however unrelated, and the prompt says
    # "lessons from earlier similar runs" about things that are not similar. Measured with
    # nomic-embed-text: a lesson about the same kind of strategy scores 0.60-0.69, a loosely
    # related one 0.44-0.52, an unrelated one 0.31-0.49, so 0.5 keeps the first two and drops
    # the last. Other embedding models score on different scales (OpenAI's are lower across
    # the board): re-tune this when MEMORY_EMBEDDING_MODEL changes. -1 disables the floor.
    memory_recall_min_score: float = Field(default=0.5, ge=-1.0, le=1.0)

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

    # Phase 17 real-world testing pass: base URL for Prometheus's own HTTP
    # query API (GET {prometheus_url}/api/v1/query), used by
    # src.observability.vitals to compute a real trailing-window
    # order-dispatch-latency percentile (histogram_quantile over
    # rate(...)) at request time -- the same query Grafana itself would
    # run, not a client-side re-derivation from raw bucket samples.
    # Defaults to localhost for local `uvicorn` dev, same convention as
    # temporal_address above; docker-compose.yml overrides this to the
    # `prometheus` service's in-network hostname for the container. If
    # Prometheus is unreachable, the vitals endpoint reports both
    # percentiles as `None` rather than a fabricated number.
    prometheus_url: str = "http://localhost:9090"

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

    # Settings redesign (broker OAuth, src.api.routes.broker_oauth): the
    # public origin this app is actually reachable at, used to build the
    # broker OAuth redirect/callback URL an operator pastes into their
    # Zerodha/Upstox developer console. Left unset by default and derived
    # from the live request's own Host/X-Forwarded-* headers instead --
    # correct for the common case (direct exposure, or a reverse proxy
    # that forwards those headers, e.g. Dockerfile.allinone's bundled
    # nginx). Set this explicitly only when that derivation is wrong for a
    # given deployment (a proxy that doesn't forward the original
    # scheme/host), the same escape hatch NEXT_PUBLIC_API_URL/CORS_ORIGINS
    # already are for their own analogous problem.
    public_base_url: str | None = None

    # Where the browser lands after a broker OAuth login (the Settings
    # page). Unset = same origin as this backend, right for the
    # single-origin all-in-one image; docker-compose.yml sets it to the
    # frontend's published port, since this backend's own /settings 404s.
    frontend_base_url: str | None = None

    # Paper trading runs on REAL market data only. With this off (the default) a missing broker
    # or too little ingested history halts the affected subscription ("awaiting data") instead of
    # quietly trading on a random walk. On, it restores the old generated-data fallbacks -- for
    # demos and the automated tests, never for a session whose results are meant to be believed.
    paper_trading_synthetic: bool = False

    # How a paper fill is charged. "cnc" is delivery (positions held overnight: the daily-signal
    # strategies), "mis" is intraday; they differ in brokerage, STT and stamp duty
    # (src.engine.backtest.friction).
    paper_trading_product: Literal["cnc", "mis"] = "cnc"
    # What the live account started with, in rupees. Live equity is this plus what the ledger of
    # confirmed fills has realized (net of estimated costs) plus open positions at the broker's
    # quote. Unset, live equity is "cannot be measured" and the live kill switch is not judged.
    live_starting_capital: float | None = None

    @field_validator("live_starting_capital", mode="before")
    @classmethod
    def _blank_live_starting_capital_is_unset(cls, value: object) -> object:
        # docker-compose forwards an unset variable as an empty string.
        return None if isinstance(value, str) and not value.strip() else value

    # A paper fill walks the broker's live depth; a snapshot older than this is refused (the
    # order waits for a fresh one) rather than filled against a book that may have moved. The
    # tick job refreshes it every few seconds.
    paper_depth_max_age_seconds: float = 30.0
    # The oldest a tick may be when the drain job acts on it. Quotes are polled every 3 s and
    # drained every 5 s, so a tick is routinely up to ~8 s old (plus the broker's round trip) by
    # design; this is the bound beyond which the feed has stalled and the price is no longer one
    # to trade or stop out on. It is not the 100 ms WebSocket-relay guard, which would discard
    # nearly every polled tick (found by measuring the cadence; see docs/CLAUDE.md, Phase 7).
    paper_tick_max_age_seconds: float = 20.0
    # How old the newest price may be before it counts as stale to a person: the Paper Desk's
    # "stale price" notice and the engine-alert watcher use it, and so does a manual close, which
    # will not use a tick older than this. One setting, so what the screen says is stale and what
    # the alert says is stale cannot differ (the console mirrors the default in
    # lib/paper-trading.ts, pinned by a shared test fixture).
    paper_stale_price_seconds: float = 120.0
    # The newest ticks kept per symbol stream (approximate trim). At one tick per 3 seconds over a
    # 6.25-hour session this is ~7,500 a day, so the default keeps about a day; a consumer that
    # falls further behind than that loses the oldest ticks, not the newest.
    paper_tick_stream_maxlen: int = 10_000

    # Futures. The margin is an APPROXIMATION (a flat share of notional, not SPAN); an entry is
    # refused when it would commit more than the stated share of the subscription's capital.
    paper_futures_margin_pct: float = 15.0
    paper_futures_max_margin_utilisation_pct: float = 50.0
    # A future is rolled into the next month this many calendar days before it expires.
    paper_futures_roll_days: int = 3

    # Options. A new position is only opened in a contract with more than this many days to
    # expiry (a near-dated option decays too fast to be worth buying), and any position is
    # squared off this many days before its expiry (1 = the day before, in market hours). A
    # position still open after its expiry is closed at the last real quote seen.
    paper_options_min_days_to_expiry: int = 3
    paper_options_squareoff_days: int = 1
    # The legs chosen for an entry are quoted for at most this long while waiting for their
    # books; past it the choice is made again from the then-current spot.
    paper_options_plan_ttl_seconds: int = 300
    # Any `src.engine.backtest.friction.FrictionModel` field, as JSON (e.g.
    # {"futures_stt_sell_pct": 0.0002}): the cost rates are published figures that change, so a
    # new rate is a setting, not a code change.
    paper_friction_overrides: dict[str, float] = Field(default_factory=dict)

    # LLM provider credentials (Settings redesign): a sibling encrypted
    # store to secrets_store_path above, same Fernet key, same
    # write-only/never-logged contract -- kept as its own file rather than
    # reusing the broker one so the two domains (broker vs. LLM provider
    # secrets) stay independently readable/rotatable, matching this
    # codebase's existing convention of one store per secret domain (see
    # notification_channel_store_path below).
    llm_provider_credentials_store_path: str = "secrets/llm_provider_credentials.enc"

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

    # Risk-free rate assumption for src.engine.options_pricing's Black-
    # Scholes implied-volatility solver (Zerodha's live option chain --
    # Kite Connect has no options-greeks field of any kind, unlike
    # Upstox's real broker-reported IV). 7% roughly tracks India's own
    # short-term government-securities yield at the time this build was
    # written; every options-IV solver needs some rate assumption, and
    # this one is a documented, configurable modeling input, never
    # presented as a measured figure the way a real quote is.
    risk_free_rate: float = 0.07

    # Phase 12 (Build Spec §18): encrypted local store for notification
    # channel config (Telegram/Discord/Slack bot tokens, webhook URLs,
    # inbound signing secrets, verified-sender allowlists) -- same Fernet
    # pattern and same encryption key as Phase 8's broker-credentials
    # store, just a separate file so the two domains never share one
    # ciphertext blob. Relative to CWD, same convention as
    # secrets_store_path above.
    notification_channel_store_path: str = "secrets/notification_channels.enc"

    # Engine alerts (src.orchestration.engine_alerts). A condition the watcher sees is announced
    # only once it has lasted this long, so a price that is late for one poll does not message
    # anyone; a critical one (a position nobody is watching) is announced at once.
    engine_alert_confirm_seconds: float = 120.0
    # Alertmanager (the compose service Prometheus sends its alerts to) posts them back to
    # POST /api/v1/webhooks/alertmanager with a bearer token. The token is created on first boot
    # in this file, on a volume only the backend writes and Alertmanager reads, kept apart from
    # the encrypted stores. An empty path turns the webhook off (it then refuses every call).
    alertmanager_token_path: str = "alertmanager_token/webhook_token"
    # Where the backend asks whether Alertmanager is up (Settings shows the answer). Empty = not
    # part of this deployment.
    alertmanager_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
