"""FastAPI application entrypoint for the Workspace Orchestrator."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, query, sync, ws
from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open a shared async Redis client for the app's lifetime."""
    settings = get_settings()
    app.state.redis = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield
    finally:
        await app.state.redis.aclose()


app = FastAPI(
    title="Workspace Orchestrator",
    version="1.0.0",
    lifespan=lifespan,
)

# Wide-open CORS: this is a local/demo service, not a public multi-origin API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(query.router, prefix="/api/v1")
app.include_router(sync.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(ws.router, prefix="/api/v1")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
