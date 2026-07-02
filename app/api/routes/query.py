"""Natural-language query endpoint: run one turn through the pipeline."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import QueryRequest, QueryResponse
from app.core.pipeline import QueryPipeline
from app.db.models import User
from app.db.session import get_db

router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryResponse)
async def handle_query(
    body: QueryRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> QueryResponse:
    """Classify, plan, execute and synthesize a response for ``body.query``."""
    pipeline = QueryPipeline(user, session, request.app.state.redis)
    result = await pipeline.handle(body.query, body.conversation_id)
    return QueryResponse(**result)
