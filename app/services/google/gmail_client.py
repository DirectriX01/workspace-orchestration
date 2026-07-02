"""Real Gmail client wrapping the synchronous Google API in async methods."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.services.google import _with_retry
from app.services.google.oauth import credentials_from_user

if TYPE_CHECKING:
    from app.db.models import User


def _decode_body_data(data: str) -> str:
    """Decode a base64url-encoded Gmail body part to text."""
    raw = base64.urlsafe_b64decode(data.encode("ascii"))
    return raw.decode("utf-8", errors="replace")


def _find_part(payload: dict[str, Any], mime_type: str) -> str | None:
    """Recursively find the first body part with ``mime_type`` and decode it."""
    if payload.get("mimeType") == mime_type:
        data = payload.get("body", {}).get("data")
        if data:
            return _decode_body_data(data)
    for part in payload.get("parts") or []:
        found = _find_part(part, mime_type)
        if found is not None:
            return found
    return None


def _extract_body(message: dict[str, Any]) -> str:
    """Return plain-text body: prefer text/plain, strip text/html, else snippet."""
    payload = message.get("payload", {})
    plain = _find_part(payload, "text/plain")
    if plain is not None:
        return plain.strip()
    html = _find_part(payload, "text/html")
    if html is not None:
        return BeautifulSoup(html, "html.parser").get_text(separator=" ").strip()
    return message.get("snippet", "")


def _normalize(message: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw Gmail ``users.messages.get`` response to the contract shape."""
    payload = message.get("payload", {})
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in payload.get("headers", [])
    }
    from_name, from_email = parseaddr(headers.get("from", ""))
    to_emails = [addr for _, addr in getaddresses([headers.get("to", "")]) if addr]
    internal_ms = message.get("internalDate")
    received_at = (
        datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
        if internal_ms
        else None
    )
    return {
        "id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "subject": headers.get("subject", ""),
        "from_name": from_name,
        "from_email": from_email,
        "to": to_emails,
        "labels": list(message.get("labelIds", [])),
        "body": _extract_body(message),
        "received_at": received_at,
    }


def _build_raw(to: list[str], subject: str, body: str) -> str:
    """Build a base64url-encoded RFC 2822 message for the Gmail API."""
    mime = EmailMessage()
    mime["To"] = ", ".join(to)
    mime["Subject"] = subject
    mime.set_content(body)
    return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")


class GmailClient:
    """Async Gmail client backed by the real Google API."""

    def __init__(self, user: "User") -> None:
        self._user = user
        self._service: Any = None

    def _svc(self) -> Any:
        if self._service is None:
            creds = credentials_from_user(self._user)
            self._service = build(
                "gmail", "v1", credentials=creds, cache_discovery=False
            )
        return self._service

    async def list_messages(
        self, updated_after: datetime | None = None, max_results: int = 500
    ) -> list[dict]:
        svc = self._svc()
        query = (
            f"after:{int(updated_after.timestamp())}"
            if updated_after is not None
            else None
        )
        message_ids: list[str] = []
        page_token: str | None = None
        while len(message_ids) < max_results:
            remaining = max_results - len(message_ids)

            def _list(token: str | None = page_token, batch: int = remaining) -> dict:
                return (
                    svc.users()
                    .messages()
                    .list(
                        userId="me",
                        q=query,
                        maxResults=min(batch, 500),
                        pageToken=token,
                    )
                    .execute()
                )

            response = await _with_retry(_list)
            message_ids.extend(m["id"] for m in response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        results: list[dict] = []
        for message_id in message_ids[:max_results]:
            results.append(await self.get_message(message_id))
        return results

    async def get_message(self, message_id: str) -> dict:
        svc = self._svc()

        def _get() -> dict:
            return (
                svc.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )

        try:
            message = await _with_retry(_get)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise KeyError(f"{message_id} not found") from None
            raise
        return _normalize(message)

    async def send_message(self, to: list[str], subject: str, body: str) -> dict:
        svc = self._svc()
        raw = _build_raw(to, subject, body)

        def _send() -> dict:
            return (
                svc.users()
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )

        sent = await _with_retry(_send)
        return {
            "id": sent.get("id", ""),
            "thread_id": sent.get("threadId", ""),
            "labels": list(sent.get("labelIds", [])),
            "status": "sent",
            "to": list(to),
            "subject": subject,
            "body": body,
        }

    async def create_draft(self, to: list[str], subject: str, body: str) -> dict:
        svc = self._svc()
        raw = _build_raw(to, subject, body)

        def _create() -> dict:
            return (
                svc.users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw}})
                .execute()
            )

        draft = await _with_retry(_create)
        return {
            "id": draft.get("id", ""),
            "message_id": draft.get("message", {}).get("id", ""),
            "status": "drafted",
            "to": list(to),
            "subject": subject,
            # Echo the drafted body so the pipeline can carry it into the
            # confirmable send params; the Gmail API does not return it.
            "body": body,
        }

    async def update_labels(
        self, message_id: str, add: list[str], remove: list[str]
    ) -> dict:
        svc = self._svc()

        def _modify() -> dict:
            return (
                svc.users()
                .messages()
                .modify(
                    userId="me",
                    id=message_id,
                    body={"addLabelIds": add, "removeLabelIds": remove},
                )
                .execute()
            )

        try:
            result = await _with_retry(_modify)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise KeyError(f"{message_id} not found") from None
            raise
        return {
            "id": message_id,
            "labels": list(result.get("labelIds", [])),
            "status": "updated",
        }
