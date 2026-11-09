import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any

import redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from src.ml_worker.trainer import TAiresTrainerService, TrainingConfig
from src.ml_worker.vector_search import build_tire_search_text
from src.server.core.celery_app import REDIS_URL, celery_app
from src.server.core.database import SQLALCHEMY_DATABASE_URL
from src.server.models.ml_training import MLTrainingJob, TrainingStatusEnum
from src.server.models.procurement import AuditActionEnum, FeedbackAuditLog, RawOfferItem
from src.server.models.references import Pattern
from src.server.models.tire import Tire

logger = logging.getLogger("celery.trainer")

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

trainer_engine = create_async_engine(SQLALCHEMY_DATABASE_URL, poolclass=NullPool)
TrainerSessionLocal = async_sessionmaker(
    bind=trainer_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


def publish_training_telemetry(job_id: int, payload: dict[str, Any]) -> None:
    """Публикует событие телеметрии обучения в Redis-канал training:{job_id}:telemetry."""
    channel = f"training:{job_id}:telemetry"
    redis_client.publish(channel, json.dumps(payload, ensure_ascii=False))


async def collect_training_pairs_from_db(db: AsyncSession) -> list[tuple[str, str]]:
    """
    Собирает обучающие пары (сырое наименование поставщика -> эталонное описание Tire)
    из журнала ручных корректировок FeedbackAuditLog и каталога шин.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    # 1. Реальные исправления закупщика из FeedbackAuditLog
    audit_stmt = (
        select(FeedbackAuditLog, RawOfferItem, Tire)
        .join(RawOfferItem, FeedbackAuditLog.item_id == RawOfferItem.id)
        .join(Tire, FeedbackAuditLog.new_tire_id == Tire.id)
        .where(FeedbackAuditLog.action == AuditActionEnum.MANUAL_REMATCH)
    )
    audit_rows = (await db.execute(audit_stmt)).all()
    for _, offer_item, tire in audit_rows:
        p_text = tire.search_text or tire.sku
        pair = (offer_item.raw_name.strip(), p_text.strip())
        if pair[0] and pair[1] and pair not in seen:
            seen.add(pair)
            pairs.append(pair)

    # 2. Каталожные пары (SKU <-> структурный search_text) для сохранения общего покрытия каталога
    tires_stmt = select(Tire).options(
        selectinload(Tire.pattern).selectinload(Pattern.manufacturer),
        selectinload(Tire.seasonality),
        selectinload(Tire.technologies),
    )
    tires = (await db.execute(tires_stmt)).scalars().all()
    for tire in tires:
        s_text = tire.search_text or build_tire_search_text(tire)
        pair = (tire.sku.strip(), s_text.strip())
        if pair[0] and pair[1] and pair not in seen:
            seen.add(pair)
            pairs.append(pair)

    return pairs


async def _run_training_job_in_db(
    job_id: int,
    explicit_pairs: list[list[str]] | None = None,
    use_feedback_log: bool = True,
    session_factory: async_sessionmaker[AsyncSession] = TrainerSessionLocal,
) -> dict[str, Any]:
    async with session_factory() as db:
        job = (
            await db.execute(select(MLTrainingJob).where(MLTrainingJob.id == job_id))
        ).scalar_one_or_none()
        if job is None:
            raise ValueError(f"MLTrainingJob id={job_id} не найдена.")

        try:
            job.status = TrainingStatusEnum.TRAINING
            await db.commit()

            pairs: list[tuple[str, str]] = []
            if explicit_pairs:
                for row in explicit_pairs:
                    if len(row) >= 2 and row[0].strip() and row[1].strip():
                        pairs.append((row[0].strip(), row[1].strip()))

            if use_feedback_log or not pairs:
                db_pairs = await collect_training_pairs_from_db(db)
                existing = set(pairs)
                for dp in db_pairs:
                    if dp not in existing:
                        pairs.append(dp)
                        existing.add(dp)

            if not pairs:
                raise ValueError(
                    "Датасет для обучения пуст: передайте dataset в запросе или заполните каталог Tire / FeedbackAuditLog."
                )

            job.dataset_size = len(pairs)
            await db.commit()

            publish_training_telemetry(
                job_id,
                {
                    "job_id": job_id,
                    "status": TrainingStatusEnum.TRAINING.value,
                    "dataset_size": len(pairs),
                    "message": f"Старт обучения {job.model_name}:{job.model_version} на {len(pairs)} примерах",
                },
            )

            cfg = TrainingConfig(
                model_name=job.model_name,
                model_version=job.model_version,
                base_model=job.base_model,
                batch_size=job.batch_size,
                epochs=job.epochs,
                learning_rate=job.learning_rate,
                val_split=job.val_split,
                pairs=pairs,
            )

            def _on_epoch(epoch_metrics: dict[str, Any]) -> None:
                publish_training_telemetry(
                    job_id,
                    {
                        "job_id": job_id,
                        "status": TrainingStatusEnum.TRAINING.value,
                        "epoch_telemetry": epoch_metrics,
                    },
                )

            result = TAiresTrainerService.train_and_publish(
                config=cfg,
                on_epoch_telemetry=_on_epoch,
            )

            job.status = TrainingStatusEnum.COMPLETED
            job.s3_uri = result.s3_uri
            job.autotest_accuracy = result.autotest_accuracy
            job.telemetry = result.telemetry
            job.model_metadata = result.metadata
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()

            publish_training_telemetry(
                job_id,
                {
                    "job_id": job_id,
                    "status": TrainingStatusEnum.COMPLETED.value,
                    "s3_uri": result.s3_uri,
                    "autotest_accuracy": result.autotest_accuracy,
                    "telemetry": result.telemetry,
                },
            )
            return {
                "job_id": job_id,
                "status": "COMPLETED",
                "s3_uri": result.s3_uri,
                "autotest_accuracy": result.autotest_accuracy,
            }

        except Exception as exc:
            await db.rollback()
            logger.exception("Ошибка в tAIres-Trainer для job_id=%s: %s", job_id, exc)
            async with session_factory() as err_db:
                err_job = (
                    await err_db.execute(select(MLTrainingJob).where(MLTrainingJob.id == job_id))
                ).scalar_one_or_none()
                if err_job is not None:
                    err_job.status = TrainingStatusEnum.FAILED
                    err_job.error_message = str(exc)
                    err_job.completed_at = datetime.now(timezone.utc)
                    await err_db.commit()

            publish_training_telemetry(
                job_id,
                {
                    "job_id": job_id,
                    "status": TrainingStatusEnum.FAILED.value,
                    "error": str(exc),
                },
            )
            raise


@celery_app.task(name="run_training_job", bind=True)
def run_training_job(
    self: object,
    job_id: int,
    explicit_pairs: list[list[str]] | None = None,
    use_feedback_log: bool = True,
) -> dict[str, Any]:
    """Фоновая задача Celery контура обучения tAIres-Trainer."""
    return asyncio.run(
        _run_training_job_in_db(
            job_id=job_id,
            explicit_pairs=explicit_pairs,
            use_feedback_log=use_feedback_log,
        )
    )
