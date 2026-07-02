"""Google OAuth 2.0 helpers: auth-URL construction, code exchange, credentials.

Uses the standard Google OAuth endpoints directly (via stdlib ``urllib``) for
the authorization URL and code exchange, and ``google-auth`` for building and
refreshing :class:`~google.oauth2.credentials.Credentials` from stored user
tokens. Client id/secret/redirect are read from application settings.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from app.config import get_settings

if TYPE_CHECKING:
    from app.db.models import User

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]


def build_auth_url(state: str | None = None) -> str:
    """Build the Google consent-screen URL (offline access, forced consent)."""
    settings = get_settings()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if state is not None:
        params["state"] = state
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Exchange an authorization ``code`` for a token dict at the token endpoint.

    Returns the raw token payload (``access_token``, ``refresh_token``,
    ``expires_in``, ``scope``, ``token_type``, ``id_token``).
    """
    settings = get_settings()
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (trusted host)
        return json.loads(response.read().decode("utf-8"))


def credentials_from_user(user: "User") -> Credentials:
    """Build OAuth credentials from a user's stored tokens, refreshing if needed."""
    settings = get_settings()
    creds = Credentials(
        token=user.google_access_token or None,
        refresh_token=user.google_refresh_token or None,
        token_uri=TOKEN_ENDPOINT,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )
    if creds.refresh_token and (not creds.token or not creds.valid):
        creds.refresh(Request())
    return creds
