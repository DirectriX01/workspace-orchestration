"""Client factory: return mock or real Google clients based on settings.

When ``get_settings().mock_google`` is true, fixture-backed mock clients are
returned (user OAuth tokens are ignored). Mock instances are cached per user
email in module-level dicts so that in-memory writes persist across calls
within a single process. Otherwise, real Google clients are constructed lazily
from the user's stored tokens.
"""

from __future__ import annotations

from app.config import get_settings
from app.db.models import User
from app.services.mock.clients import (
    MockCalendarClient,
    MockDriveClient,
    MockGmailClient,
)
from app.services.protocols import (
    CalendarClientProtocol,
    DriveClientProtocol,
    GmailClientProtocol,
)

_mock_gmail: dict[str, MockGmailClient] = {}
_mock_calendar: dict[str, MockCalendarClient] = {}
_mock_drive: dict[str, MockDriveClient] = {}


def get_gmail_client(user: User) -> GmailClientProtocol:
    """Return a Gmail client for ``user`` (mock or real per settings)."""
    if get_settings().mock_google:
        client = _mock_gmail.get(user.email)
        if client is None:
            client = MockGmailClient()
            _mock_gmail[user.email] = client
        return client
    from app.services.google.gmail_client import GmailClient

    return GmailClient(user)


def get_calendar_client(user: User) -> CalendarClientProtocol:
    """Return a Calendar client for ``user`` (mock or real per settings)."""
    if get_settings().mock_google:
        client = _mock_calendar.get(user.email)
        if client is None:
            client = MockCalendarClient()
            _mock_calendar[user.email] = client
        return client
    from app.services.google.calendar_client import CalendarClient

    return CalendarClient(user)


def get_drive_client(user: User) -> DriveClientProtocol:
    """Return a Drive client for ``user`` (mock or real per settings)."""
    if get_settings().mock_google:
        client = _mock_drive.get(user.email)
        if client is None:
            client = MockDriveClient()
            _mock_drive[user.email] = client
        return client
    from app.services.google.drive_client import DriveClient

    return DriveClient(user)
