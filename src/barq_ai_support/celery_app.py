"""
S3.3 — Celery application instance. Redis is both broker and result backend.

Run the worker locally with:
    uv run celery -A src.barq_ai_support.celery_app worker --loglevel=info

On Windows, the default prefork pool doesn't work, so add --pool=solo:
    uv run celery -A src.barq_ai_support.celery_app worker --loglevel=info --pool=solo
"""
from celery import Celery

from .config import settings

celery_app = Celery(
    "barq_ai_support",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["src.barq_ai_support.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
)