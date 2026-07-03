"""Real Google Calendar client wrapping the sync API in async methods."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import get_settings
from app.services.google import _with_retry
from app.services.google.oauth import credentials_from_user

if TYPE_CHECKING:
    from app.db.models import User


def _parse_dt(node: dict[str, Any]) -> datetime | None:
    """Parse a Calendar ``start``/``end`` node (timed ``dateTime`` or all-day ``date``)."""
    if not node:
        return None
    if node.get("dateTime"):
        raw = node["dateTime"].replace("Z", "+00:00")
        return datetime.fromisoformat(raw)
    if node.get("date"):
        tz = ZoneInfo(node.get("timeZone") or get_settings().default_tz)
        return datetime.fromisoformat(node["date"]).replace(tzinfo=tz)
    return None


def _to_iso(value: Any) -> str:
    """Coerce a datetime (or already-string) to an ISO-8601 string."""
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _normalize(event: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw Calendar event resource to the contract shape."""
    return {
        "id": event.get("id", ""),
        "title": event.get("summary", ""),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "organizer_email": event.get("organizer", {}).get("email", ""),
        "attendees": [
            a["email"] for a in event.get("attendees", []) if a.get("email")
        ],
        "start": _parse_dt(event.get("start", {})),
        "end": _parse_dt(event.get("end", {})),
        "status": event.get("status", "confirmed"),
    }


class CalendarClient:
    """Async Calendar client backed by the real Google API."""

    def __init__(self, user: "User") -> None:
        self._user = user
        self._service: Any = None

    def _svc(self) -> Any:
        if self._service is None:
            creds = credentials_from_user(self._user)
            self._service = build(
                "calendar", "v3", credentials=creds, cache_discovery=False
            )
        return self._service

    async def list_events(
        self, updated_after: datetime | None = None, max_results: int = 500
    ) -> list[dict]:
        svc = self._svc()
        events: list[dict] = []
        page_token: str | None = None
        updated_min = updated_after.isoformat() if updated_after is not None else None
        # Real calendars explode into thousands of instances under
        # singleEvents=True; without a window the max_results cap fills up
        # with stale history and upcoming events never sync. Full syncs
        # therefore window from 30 days back onward; incremental syncs
        # (updatedMin) already self-limit.
        time_min = (
            None
            if updated_after is not None
            else (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        )
        while len(events) < max_results:
            remaining = max_results - len(events)

            def _list(token: str | None = page_token, batch: int = remaining) -> dict:
                return (
                    svc.events()
                    .list(
                        calendarId="primary",
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=min(batch, 2500),
                        updatedMin=updated_min,
                        timeMin=time_min,
                        pageToken=token,
                    )
                    .execute()
                )

            response = await _with_retry(_list)
            events.extend(_normalize(e) for e in response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return events[:max_results]

    async def get_event(self, event_id: str) -> dict:
        svc = self._svc()

        def _get() -> dict:
            return svc.events().get(calendarId="primary", eventId=event_id).execute()

        try:
            event = await _with_retry(_get)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise KeyError(f"{event_id} not found") from None
            raise
        return _normalize(event)

    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        attendees: list[str],
        description: str = "",
        location: str = "",
    ) -> dict:
        svc = self._svc()
        body = {
            "summary": title,
            "description": description,
            "location": location,
            "start": {"dateTime": _to_iso(start)},
            "end": {"dateTime": _to_iso(end)},
            "attendees": [{"email": email} for email in attendees],
        }

        def _insert() -> dict:
            return (
                svc.events()
                .insert(calendarId="primary", body=body, sendUpdates="all")
                .execute()
            )

        return _normalize(await _with_retry(_insert))

    async def update_event(self, event_id: str, changes: dict) -> dict:
        svc = self._svc()
        body: dict[str, Any] = {}
        if "title" in changes:
            body["summary"] = changes["title"]
        if "description" in changes:
            body["description"] = changes["description"]
        if "location" in changes:
            body["location"] = changes["location"]
        if "status" in changes:
            body["status"] = changes["status"]
        if "start" in changes:
            body["start"] = {"dateTime": _to_iso(changes["start"])}
        if "end" in changes:
            body["end"] = {"dateTime": _to_iso(changes["end"])}
        if "attendees" in changes:
            body["attendees"] = [{"email": email} for email in changes["attendees"]]

        def _patch() -> dict:
            return (
                svc.events()
                .patch(calendarId="primary", eventId=event_id, body=body)
                .execute()
            )

        try:
            event = await _with_retry(_patch)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise KeyError(f"{event_id} not found") from None
            raise
        return _normalize(event)

    async def delete_event(self, event_id: str) -> dict:
        svc = self._svc()

        def _delete() -> Any:
            return (
                svc.events().delete(calendarId="primary", eventId=event_id).execute()
            )

        try:
            await _with_retry(_delete)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise KeyError(f"{event_id} not found") from None
            raise
        return {"id": event_id, "status": "deleted"}
