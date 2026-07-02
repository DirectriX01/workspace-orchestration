"""Google OAuth endpoints: consent redirect and token-exchange callback."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import User
from app.db.session import get_db
from app.services.google.oauth import build_auth_url, exchange_code

router = APIRouter(tags=["auth"])

_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"


@router.get("/auth/google")
async def google_auth() -> RedirectResponse:
    """Redirect to Google's consent screen (400 if OAuth is unconfigured)."""
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Google OAuth not configured; set GOOGLE_CLIENT_ID/SECRET. "
                "Mock mode (MOCK_GOOGLE=true) needs no auth."
            ),
        )
    return RedirectResponse(build_auth_url())


@router.get("/auth/google/callback")
async def google_callback(
    code: str, session: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    """Exchange the auth ``code`` for tokens and upsert the user's credentials."""
    tokens = await asyncio.to_thread(exchange_code, code)
    access_token = tokens.get("access_token")
    async with httpx.AsyncClient() as client:
        response = await client.get(
            _USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        info = response.json()

    email = info["email"]
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=email)
        session.add(user)
    user.google_access_token = access_token
    user.google_refresh_token = tokens.get("refresh_token")
    await session.commit()
    return {"status": "authenticated", "email": email}
