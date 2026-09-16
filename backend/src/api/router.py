from fastapi import APIRouter

from src.api.routes import admin, agents, approvals, auth, orchestration, strategies

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(orchestration.router)
api_router.include_router(agents.router)
api_router.include_router(strategies.router)
api_router.include_router(approvals.router)
