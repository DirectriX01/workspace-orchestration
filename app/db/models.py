"""SQLAlchemy ORM models for the Workspace Orchestrator."""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class User(Base):
    """An end user with optional Google OAuth credentials."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    google_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Conversation(Base):
    """A single orchestrated user query with its intent, plan and response."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    query: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[dict | None] = mapped_column(JSONB)
    plan: Mapped[dict | None] = mapped_column(JSONB)
    response: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GmailCache(Base):
    """Cached, embedded Gmail messages for a user."""

    __tablename__ = "gmail_cache"
    __table_args__ = (UniqueConstraint("user_id", "email_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    email_id: Mapped[str] = mapped_column(String(255), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(Text)
    from_email: Mapped[str | None] = mapped_column(String(320))
    from_name: Mapped[str | None] = mapped_column(String(255))
    to_emails: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    labels: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    body_preview: Mapped[str | None] = mapped_column(Text)
    embed_text: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GcalCache(Base):
    """Cached, embedded Google Calendar events for a user."""

    __tablename__ = "gcal_cache"
    __table_args__ = (UniqueConstraint("user_id", "event_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    organizer_email: Mapped[str | None] = mapped_column(String(320))
    attendee_emails: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), default="confirmed")
    embed_text: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GdriveCache(Base):
    """Cached, embedded Google Drive files for a user."""

    __tablename__ = "gdrive_cache"
    __table_args__ = (UniqueConstraint("user_id", "file_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    owner_email: Mapped[str | None] = mapped_column(String(320))
    content_preview: Mapped[str | None] = mapped_column(Text)
    web_link: Mapped[str | None] = mapped_column(Text)
    embed_text: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncState(Base):
    """Per-user, per-service incremental sync bookkeeping."""

    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("user_id", "service"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service: Mapped[str] = mapped_column(String(20), nullable=False)
    cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="idle")
    items_synced: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
