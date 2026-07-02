"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("google_access_token", sa.Text(), nullable=True),
        sa.Column("google_refresh_token", sa.Text(), nullable=True),
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default="Asia/Kolkata",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("intent", postgresql.JSONB(), nullable=True),
        sa.Column("plan", postgresql.JSONB(), nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "gmail_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_id", sa.String(length=255), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("from_email", sa.String(length=320), nullable=True),
        sa.Column("from_name", sa.String(length=255), nullable=True),
        sa.Column("to_emails", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("labels", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("body_preview", sa.Text(), nullable=True),
        sa.Column("embed_text", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "email_id"),
    )
    op.create_index("ix_gmail_cache_user_id", "gmail_cache", ["user_id"])

    op.create_table(
        "gcal_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("organizer_email", sa.String(length=320), nullable=True),
        sa.Column("attendee_emails", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=50),
            server_default="confirmed",
            nullable=False,
        ),
        sa.Column("embed_text", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "event_id"),
    )
    op.create_index("ix_gcal_cache_user_id", "gcal_cache", ["user_id"])

    op.create_table(
        "gdrive_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("owner_email", sa.String(length=320), nullable=True),
        sa.Column("content_preview", sa.Text(), nullable=True),
        sa.Column("web_link", sa.Text(), nullable=True),
        sa.Column("embed_text", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "file_id"),
    )
    op.create_index("ix_gdrive_cache_user_id", "gdrive_cache", ["user_id"])

    op.create_table(
        "sync_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service", sa.String(length=20), nullable=False),
        sa.Column("cursor", sa.String(length=255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="idle", nullable=False),
        sa.Column("items_synced", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "service"),
    )

    # Approximate-nearest-neighbour indexes for cosine similarity search.
    op.execute(
        "CREATE INDEX ix_gmail_cache_embedding ON gmail_cache "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    op.execute(
        "CREATE INDEX ix_gcal_cache_embedding ON gcal_cache "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    op.execute(
        "CREATE INDEX ix_gdrive_cache_embedding ON gdrive_cache "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_gdrive_cache_embedding")
    op.execute("DROP INDEX IF EXISTS ix_gcal_cache_embedding")
    op.execute("DROP INDEX IF EXISTS ix_gmail_cache_embedding")

    op.drop_table("sync_state")
    op.drop_index("ix_gdrive_cache_user_id", table_name="gdrive_cache")
    op.drop_table("gdrive_cache")
    op.drop_index("ix_gcal_cache_user_id", table_name="gcal_cache")
    op.drop_table("gcal_cache")
    op.drop_index("ix_gmail_cache_user_id", table_name="gmail_cache")
    op.drop_table("gmail_cache")
    op.drop_table("conversations")
    op.drop_table("users")
