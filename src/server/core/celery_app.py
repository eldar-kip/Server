from celery import Celery
from src.server.core.config import settings

REDIS_URL: str = settings.REDIS_URL

celery_app = Celery(
    "taires_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "src.server.tasks.matching_tasks",
        "src.server.tasks.erp_tasks",
        "src.server.tasks.training_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_default_queue="celery_tasks",
    task_routes={
        "process_procurement_session": {"queue": "ml"},
        "run_training_job": {"queue": "ml"},
        "send_to_erp": {"queue": "celery_tasks"},
    },
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3300,
)


@celery_app.task(name="ping_celery")
def ping_celery() -> str:
    """Проверка работоспособности воркера Celery."""
    return "pong"
