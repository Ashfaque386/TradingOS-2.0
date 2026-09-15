from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.router import api_router
from src.api.routes.health import router as health_router
from src.core.config import get_settings
from src.observability.logging import configure_logging

configure_logging()

settings = get_settings()

app = FastAPI(title="TradingOS 2.0 API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(api_router)
