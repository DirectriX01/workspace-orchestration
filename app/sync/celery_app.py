"""Celery application for background workspace syncs.

The broker and result backend both point at Redis. Tasks live in
:mod:`app.sync.tasks` and are eagerly discoverable via ``include`` so a worker
started with ``celery -A app.sync.celery_app worker`` picks them up.
"""

from __future__ import annotations

from celery import Celery

from app.config import get_settings

_settings = get_settings()

celery_app = Celery(
    "orchestrator",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["app.sync.tasks"],
)

celery_app.conf.task_acks_late = True
celery_app.conf.worker_prefetch_multiplier = 1
celery_app.conf.task_serializer = "json"
celery_app.conf.result_serializer = "json"
celery_app.conf.accept_content = ["json"]
