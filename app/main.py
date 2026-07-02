"""FastAPI application entrypoint."""

from fastapi import FastAPI

app = FastAPI(title="Workspace Orchestrator", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
