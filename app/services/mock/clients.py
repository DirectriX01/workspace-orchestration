"""In-memory, fixture-backed mock implementations of the service clients.

These satisfy the protocols in :mod:`app.services.protocols` without touching
the network. Fixture JSON (see ``fixtures/``) uses ``days_offset`` (and, for
events, ``start_hour``/``duration_min``) which are resolved to concrete
tz-aware datetimes AT LOAD TIME against ``datetime.now`` in the configured
default timezone, so relative dates in the demo data stay stable within a run.

Reads return the normalized dict shapes from the project contract; writes
mutate a per-instance in-memory store and return realistic payloads with
deterministic, per-instance ids.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


@lru_cache(maxsize=None)
def _load_fixture(name: str) -> tuple[dict[str, Any], ...]:
    """Load and cache the raw fixture list for ``name`` (e.g. ``"emails.json"``)."""
    with (_FIXTURES_DIR / name).open("r", encoding="utf-8") as handle:
        return tuple(json.load(handle))


def _now() -> datetime:
    """Current tz-aware time in the configured default timezone."""
    return datetime.now(ZoneInfo(get_settings().default_tz))


class MockGmailClient:
    """Fixture-backed Gmail client with an in-memory message store."""

    def __init__(self) -> None:
        self._now = _now()
        self._emails: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(_load_fixture("emails.json")):
            email = self._resolve(index, raw)
            self._emails[email["id"]] = email
        self._sent: list[dict[str, Any]] = []
        self._drafts: list[dict[str, Any]] = []
        self._sent_counter = 0
        self._draft_counter = 0

    def _resolve(self, index: int, raw: dict[str, Any]) -> dict[str, Any]:
        day = self._now + timedelta(days=int(raw["days_offset"]))
        received_at = day.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(
            minutes=index
        )
        return {
            "id": raw["id"],
            "thread_id": raw.get("thread_id", raw["id"]),
            "subject": raw.get("subject", ""),
            "from_name": raw.get("from_name", ""),
            "from_email": raw.get("from_email", ""),
            "to": list(raw.get("to", [])),
            "labels": list(raw.get("labels", [])),
            "body": raw.get("body", ""),
            "received_at": received_at,
        }

    async def list_messages(
        self, updated_after: datetime | None = None, max_results: int = 500
    ) -> list[dict]:
        items = list(self._emails.values())
        if updated_after is not None:
            items = [e for e in items if e["received_at"] >= updated_after]
        items.sort(key=lambda e: e["received_at"], reverse=True)
        return [dict(e) for e in items[:max_results]]

    async def get_message(self, message_id: str) -> dict:
        try:
            return dict(self._emails[message_id])
        except KeyError:
            raise KeyError(f"{message_id} not found") from None

    async def send_message(self, to: list[str], subject: str, body: str) -> dict:
        self._sent_counter += 1
        payload = {
            "id": f"sent_{self._sent_counter:03d}",
            "thread_id": f"thr_sent_{self._sent_counter:03d}",
            "status": "sent",
            "to": list(to),
            "subject": subject,
            "body": body,
            "labels": ["SENT"],
        }
        self._sent.append(payload)
        return dict(payload)

    async def create_draft(self, to: list[str], subject: str, body: str) -> dict:
        self._draft_counter += 1
        payload = {
            "id": f"draft_{self._draft_counter:03d}",
            "status": "drafted",
            "to": list(to),
            "subject": subject,
            "body": body,
        }
        self._drafts.append(payload)
        return dict(payload)

    async def update_labels(
        self, message_id: str, add: list[str], remove: list[str]
    ) -> dict:
        try:
            email = self._emails[message_id]
        except KeyError:
            raise KeyError(f"{message_id} not found") from None
        to_remove = set(remove)
        labels = [label for label in email["labels"] if label not in to_remove]
        for label in add:
            if label not in labels:
                labels.append(label)
        email["labels"] = labels
        return {"id": message_id, "labels": list(labels), "status": "updated"}


class MockCalendarClient:
    """Fixture-backed Calendar client with an in-memory event store."""

    def __init__(self) -> None:
        self._now = _now()
        self._events: dict[str, dict[str, Any]] = {}
        for raw in _load_fixture("events.json"):
            event = self._resolve(raw)
            self._events[event["id"]] = event
        self._counter = 0

    def _resolve(self, raw: dict[str, Any]) -> dict[str, Any]:
        day = self._now + timedelta(days=int(raw["days_offset"]))
        start = day.replace(
            hour=int(raw.get("start_hour", 9)), minute=0, second=0, microsecond=0
        )
        end = start + timedelta(minutes=int(raw.get("duration_min", 60)))
        return {
            "id": raw["id"],
            "title": raw.get("title", ""),
            "description": raw.get("description", ""),
            "location": raw.get("location", ""),
            "organizer_email": raw.get("organizer_email", ""),
            "attendees": list(raw.get("attendees", [])),
            "start": start,
            "end": end,
            "status": raw.get("status", "confirmed"),
        }

    async def list_events(
        self, updated_after: datetime | None = None, max_results: int = 500
    ) -> list[dict]:
        items = list(self._events.values())
        if updated_after is not None:
            items = [e for e in items if e["start"] >= updated_after]
        items.sort(key=lambda e: e["start"])
        return [dict(e) for e in items[:max_results]]

    async def get_event(self, event_id: str) -> dict:
        try:
            return dict(self._events[event_id])
        except KeyError:
            raise KeyError(f"{event_id} not found") from None

    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        attendees: list[str],
        description: str = "",
        location: str = "",
    ) -> dict:
        self._counter += 1
        event = {
            "id": f"evt_new_{self._counter:03d}",
            "title": title,
            "description": description,
            "location": location,
            "organizer_email": get_settings().demo_user_email,
            "attendees": list(attendees),
            "start": start,
            "end": end,
            "status": "confirmed",
        }
        self._events[event["id"]] = event
        return dict(event)

    async def update_event(self, event_id: str, changes: dict) -> dict:
        try:
            event = self._events[event_id]
        except KeyError:
            raise KeyError(f"{event_id} not found") from None
        for key in ("title", "description", "location", "start", "end", "status"):
            if key in changes:
                event[key] = changes[key]
        if "attendees" in changes:
            event["attendees"] = list(changes["attendees"])
        return dict(event)

    async def delete_event(self, event_id: str) -> dict:
        if event_id not in self._events:
            raise KeyError(f"{event_id} not found")
        del self._events[event_id]
        return {"id": event_id, "status": "deleted"}


class MockDriveClient:
    """Fixture-backed Drive client with an in-memory file store."""

    def __init__(self) -> None:
        self._now = _now()
        self._files: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(_load_fixture("files.json")):
            file = self._resolve(index, raw)
            self._files[file["id"]] = file
        self._parents: dict[str, str | None] = {}
        self._shares: list[dict[str, Any]] = []
        self._folder_counter = 0
        self._share_counter = 0

    def _resolve(self, index: int, raw: dict[str, Any]) -> dict[str, Any]:
        day = self._now + timedelta(days=int(raw["days_offset"]))
        modified_at = day.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(
            minutes=index
        )
        return {
            "id": raw["id"],
            "name": raw.get("name", ""),
            "mime_type": raw.get("mime_type", ""),
            "owner_email": raw.get("owner_email", ""),
            "content": raw.get("content", ""),
            "web_link": raw.get("web_link", ""),
            "modified_at": modified_at,
        }

    async def list_files(
        self, updated_after: datetime | None = None, max_results: int = 500
    ) -> list[dict]:
        items = list(self._files.values())
        if updated_after is not None:
            items = [f for f in items if f["modified_at"] >= updated_after]
        items.sort(key=lambda f: f["modified_at"], reverse=True)
        return [dict(f) for f in items[:max_results]]

    async def get_file(self, file_id: str) -> dict:
        try:
            return dict(self._files[file_id])
        except KeyError:
            raise KeyError(f"{file_id} not found") from None

    async def share_file(self, file_id: str, email: str, role: str = "reader") -> dict:
        if file_id not in self._files:
            raise KeyError(f"{file_id} not found")
        self._share_counter += 1
        share = {
            "id": file_id,
            "permission_id": f"perm_{self._share_counter:03d}",
            "email": email,
            "role": role,
            "status": "shared",
        }
        self._shares.append(share)
        return dict(share)

    async def create_folder(self, name: str, parent_id: str | None = None) -> dict:
        self._folder_counter += 1
        folder_id = f"folder_new_{self._folder_counter:03d}"
        folder = {
            "id": folder_id,
            "name": name,
            "mime_type": "application/vnd.google-apps.folder",
            "owner_email": get_settings().demo_user_email,
            "content": "",
            "web_link": f"https://drive.google.com/drive/folders/{folder_id}",
            "modified_at": self._now,
        }
        self._files[folder_id] = folder
        self._parents[folder_id] = parent_id
        return dict(folder)

    async def move_file(self, file_id: str, folder_id: str) -> dict:
        if file_id not in self._files:
            raise KeyError(f"{file_id} not found")
        self._parents[file_id] = folder_id
        return {"id": file_id, "folder_id": folder_id, "status": "moved"}
