"""Structural (``typing.Protocol``) interfaces for workspace service clients.

Both the mock clients (:mod:`app.services.mock.clients`) and the real Google
clients (:mod:`app.services.google`) conform to these protocols, so callers can
depend on the protocol rather than a concrete implementation. Every method is
asynchronous and returns the normalized dict shapes defined in the project
contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class GmailClientProtocol(Protocol):
    """Async Gmail operations returning normalized ``email`` dicts."""

    async def list_messages(
        self, updated_after: datetime | None = None, max_results: int = 500
    ) -> list[dict]:
        """Return normalized emails, optionally only those newer than ``updated_after``."""
        ...

    async def get_message(self, message_id: str) -> dict:
        """Return one normalized email; raise ``KeyError`` if unknown."""
        ...

    async def send_message(self, to: list[str], subject: str, body: str) -> dict:
        """Send an email and return a realistic send-result payload."""
        ...

    async def create_draft(self, to: list[str], subject: str, body: str) -> dict:
        """Create a draft and return a realistic draft payload."""
        ...

    async def update_labels(
        self, message_id: str, add: list[str], remove: list[str]
    ) -> dict:
        """Add/remove labels on a message and return the updated label state."""
        ...


@runtime_checkable
class CalendarClientProtocol(Protocol):
    """Async Google Calendar operations returning normalized ``event`` dicts."""

    async def list_events(
        self, updated_after: datetime | None = None, max_results: int = 500
    ) -> list[dict]:
        """Return normalized events, optionally filtered by ``updated_after``."""
        ...

    async def get_event(self, event_id: str) -> dict:
        """Return one normalized event; raise ``KeyError`` if unknown."""
        ...

    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        attendees: list[str],
        description: str = "",
        location: str = "",
    ) -> dict:
        """Create an event and return the full normalized event."""
        ...

    async def update_event(self, event_id: str, changes: dict) -> dict:
        """Merge ``changes`` into an event and return the updated normalized event."""
        ...

    async def delete_event(self, event_id: str) -> dict:
        """Delete an event and return a realistic deletion payload."""
        ...


@runtime_checkable
class DriveClientProtocol(Protocol):
    """Async Google Drive operations returning normalized ``file`` dicts."""

    async def list_files(
        self, updated_after: datetime | None = None, max_results: int = 500
    ) -> list[dict]:
        """Return normalized files, optionally filtered by ``updated_after``."""
        ...

    async def get_file(self, file_id: str) -> dict:
        """Return one normalized file (with content); raise ``KeyError`` if unknown."""
        ...

    async def share_file(self, file_id: str, email: str, role: str = "reader") -> dict:
        """Share a file with ``email`` and return a realistic share payload."""
        ...

    async def create_folder(self, name: str, parent_id: str | None = None) -> dict:
        """Create a folder and return the full normalized file/folder."""
        ...

    async def move_file(self, file_id: str, folder_id: str) -> dict:
        """Move a file into a folder and return a realistic move payload."""
        ...
