import os
from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "taires_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["src.server.tasks.matching_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_default_queue="celery_tasks",
    task_track_started=True,
    task_time_limit=1800,
    task_soft_time_limit=1500,
)


@celery_app.task(name="ping_celery")
def ping_celery() -> str:
    return "pong"
