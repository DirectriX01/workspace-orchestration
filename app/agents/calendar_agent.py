"""Calendar agent: search, event fetch, and conflict-aware create/update/delete.

Mutating operations run a conflict check against cached events *before* calling
the client and refuse to write when the new time window overlaps an existing
(non-cancelled) event. Successful creates/updates are written through to
``gcal_cache`` (re-embedded) so subsequent searches see them immediately;
deletes remove the cached row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar

from dateutil import parser as dateutil_parser
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agents.base import BaseAgent
from app.db.models import GcalCache


def _parse_iso(value: Any) -> datetime | None:
    """Coerce an ISO string (or passthrough datetime) to a datetime, defensively."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return dateutil_parser.isoparse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None


class CalendarAgent(BaseAgent):
    """Adapter over the hybrid searcher and a normalized Calendar client."""

    name: ClassVar[str] = "calendar"

    async def search(self, params: dict) -> dict:
        rows = await self.deps.searcher.search_gcal(self.deps.user.id, params)
        return self._search_result(rows)

    async def get_context(self, params: dict) -> dict:
        event = await self._load_event(params.get("event_id") or params.get("id"))
        if event is None:
            return {"status": "empty", "results": []}
        return {"status": "ok", "results": [event]}

    async def execute(self, action: str, params: dict) -> dict:
        if action == "create_event":
            return await self._create_event(params)
        if action == "update_event":
            return await self._update_event(params)
        if action == "delete_event":
            return await self._delete_event(params)
        raise ValueError(f"unknown calendar action: {action}")

    # ------------------------------------------------------------------ #
    # Mutations                                                          #
    # ------------------------------------------------------------------ #
    async def _create_event(self, params: dict) -> dict:
        start = _parse_iso(params.get("start"))
        end = _parse_iso(params.get("end"))
        if start is not None and end is not None:
            overlaps = await self.deps.searcher.find_overlapping(
                self.deps.user.id, start, end, exclude_event_id=None
            )
            if overlaps:
                return {"status": "conflict", "conflicts": overlaps}
        event = await self.deps.client.create_event(
            title=params.get("title", "") or "",
            start=start,
            end=end,
            attendees=self._as_list(params.get("attendees")),
            description=params.get("description", "") or "",
            location=params.get("location", "") or "",
        )
        await self._write_through(event)
        return self._ok(event)

    async def _update_event(self, params: dict) -> dict:
        event_id = params["event_id"]
        changes: dict[str, Any] = dict(params.get("changes") or {})
        new_start = _parse_iso(changes.get("start")) if changes.get("start") else None
        new_end = _parse_iso(changes.get("end")) if changes.get("end") else None

        if new_start is not None or new_end is not None:
            existing = await self._load_event(event_id)
            start = new_start or (
                _parse_iso(existing.get("start")) if existing else None
            )
            end = new_end or (_parse_iso(existing.get("end")) if existing else None)
            if start is not None and end is not None:
                overlaps = await self.deps.searcher.find_overlapping(
                    self.deps.user.id, start, end, exclude_event_id=event_id
                )
                if overlaps:
                    return {"status": "conflict", "conflicts": overlaps}

        # Pass datetime objects (not ISO strings) to the client.
        if new_start is not None:
            changes["start"] = new_start
        if new_end is not None:
            changes["end"] = new_end
        event = await self.deps.client.update_event(event_id, changes)
        await self._write_through(event)
        return self._ok(event)

    async def _delete_event(self, params: dict) -> dict:
        event_id = params["event_id"]
        payload = await self.deps.client.delete_event(event_id)
        session = self.deps.searcher.session
        await session.execute(
            delete(GcalCache).where(
                GcalCache.user_id == self.deps.user.id,
                GcalCache.event_id == event_id,
            )
        )
        await session.commit()
        return self._ok(payload)

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    async def _load_event(self, event_id: str | None) -> dict | None:
        """Cache-first single-event fetch, falling back to the client."""
        if not event_id:
            return None
        cached = await self.deps.searcher.get_by_source_id(
            "gcal", self.deps.user.id, event_id
        )
        if cached is not None:
            return cached
        try:
            doc = await self.deps.client.get_event(event_id)
        except KeyError:
            return None
        result = self._jsonify(dict(doc))
        result["source"] = "gcal"
        return result

    async def _write_through(self, event: dict) -> None:
        """Re-embed and upsert a freshly created/updated event into the cache."""
        # Imported lazily: app.search.embed_texts is authored concurrently and
        # only needed at mutation time, so module import stays dependency-free.
        from app.search.embed_texts import event_embed_text

        embed_text = event_embed_text(event)
        vector = await self.deps.embedder.embed(embed_text)
        values = {
            "user_id": self.deps.user.id,
            "event_id": event["id"],
            "title": event.get("title"),
            "description": event.get("description"),
            "location": event.get("location"),
            "organizer_email": event.get("organizer_email"),
            "attendee_emails": list(event.get("attendees") or []),
            "start_time": _parse_iso(event.get("start")),
            "end_time": _parse_iso(event.get("end")),
            "status": event.get("status", "confirmed"),
            "embed_text": embed_text,
            "embedding": vector,
            "updated_at": datetime.now(timezone.utc),
        }
        update_cols = {
            key: val for key, val in values.items() if key not in ("user_id", "event_id")
        }
        stmt = pg_insert(GcalCache).values(**values).on_conflict_do_update(
            index_elements=["user_id", "event_id"], set_=update_cols
        )
        session = self.deps.searcher.session
        await session.execute(stmt)
        await session.commit()
