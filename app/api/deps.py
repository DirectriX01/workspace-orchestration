"""Shared FastAPI dependencies for the API layer."""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import User
from app.db.session import get_db


async def get_current_user(
    request: Request, session: AsyncSession = Depends(get_db)
) -> User:
    """Resolve the current user from the ``X-User-Email`` header.

    Falls back to the configured demo user when the header is absent. The row
    is created (and committed) on first sight so the demo works without an
    explicit sign-up step.
    """
    settings = get_settings()
    email = request.headers.get("X-User-Email") or settings.demo_user_email
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=email)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user
