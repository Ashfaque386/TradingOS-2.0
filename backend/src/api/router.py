from fastapi import APIRouter

from src.api.routes import (
    admin,
    agents,
    approvals,
    auth,
    backtests,
    broker_credentials,
    kill_switch,
    live_trading,
    market_data,
    orchestration,
    paper_trading,
    risk,
    risk_limits,
    shadow_mode,
    strategies,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(orchestration.router)
api_router.include_router(agents.router)
api_router.include_router(strategies.router)
api_router.include_router(approvals.router)
api_router.include_router(backtests.router)
api_router.include_router(kill_switch.router)
api_router.include_router(risk_limits.router)
api_router.include_router(risk.router)
api_router.include_router(paper_trading.router)
api_router.include_router(broker_credentials.router)
api_router.include_router(shadow_mode.router)
api_router.include_router(live_trading.router)
api_router.include_router(market_data.router)
