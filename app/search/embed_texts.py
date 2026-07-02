"""Builders that turn normalized workspace records into embedding source text.

Each builder produces a compact, human-readable block that is fed to the
embedding model (and stored in the ``embed_text`` column) for a Gmail message,
a Calendar event or a Drive file. Email bodies are cleaned first: HTML is
stripped and quoted-reply cruft (``>`` lines and ``On ... wrote:`` headers) is
dropped so the vector reflects the new content rather than the thread history.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

#: Matches the "On <date> <person> wrote:" header that precedes a quoted reply.
_QUOTE_HEADER_RE = re.compile(r"^On\b.*\bwrote:\s*$", re.IGNORECASE)


def _fmt_dt(value: Any) -> str:
    """Render a datetime (or already-stringified value) for embedding text."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def clean_email_body(body: str | None) -> str:
    """Strip HTML and quoted-reply lines from an email body.

    HTML is flattened to text (only when the body actually contains a ``<``),
    then any line beginning with ``>`` or matching an ``On ... wrote:`` quote
    header is removed. Also used to build the stored ``body_preview``.
    """
    if not body:
        return ""
    text = body
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(separator="\n")
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if _QUOTE_HEADER_RE.match(stripped):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def email_embed_text(email: dict) -> str:
    """Build embedding source text for a normalized Gmail message dict."""
    body = clean_email_body(email.get("body", ""))[:6000]
    return (
        f"From: {email.get('from_name', '')} <{email.get('from_email', '')}>\n"
        f"Subject: {email.get('subject', '')}\n"
        f"Date: {_fmt_dt(email.get('received_at'))}\n\n"
        f"{body}"
    )


def event_embed_text(event: dict) -> str:
    """Build embedding source text for a normalized Calendar event dict."""
    attendees = ", ".join(event.get("attendees") or [])
    description = (event.get("description") or "")[:2000]
    return (
        f"Event: {event.get('title', '')}\n"
        f"When: {_fmt_dt(event.get('start'))} - {_fmt_dt(event.get('end'))}\n"
        f"Where: {event.get('location', '')}\n"
        f"Attendees: {attendees}\n\n"
        f"{description}"
    )


def file_embed_text(file: dict) -> str:
    """Build embedding source text for a normalized Drive file dict."""
    content = (file.get("content") or "")[:4000]
    return (
        f"File: {file.get('name', '')}\n"
        f"Type: {file.get('mime_type', '')}\n"
        f"Owner: {file.get('owner_email', '')}\n"
        f"Modified: {_fmt_dt(file.get('modified_at'))}\n\n"
        f"{content}"
    )
