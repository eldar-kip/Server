import asyncio
import json
import logging
import redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool

from src.server.core.celery_app import celery_app, REDIS_URL
from src.server.core.database import SQLALCHEMY_DATABASE_URL
from src.server.models.procurement import ProcurementSession, RawOfferItem, TaskStatusEnum

logger = logging.getLogger("celery.task")

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

worker_engine = create_async_engine(SQLALCHEMY_DATABASE_URL, poolclass=NullPool)
WorkerSessionLocal = async_sessionmaker(
    bind=worker_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

BATCH_SIZE = 50


def publish_progress(
    session_id: int,
    percent: int,
    status: TaskStatusEnum,
    matched: int,
    total: int,
) -> None:
    """Публикация прогресса в Redis-канал session:{id}:progress."""
    channel = f"session:{session_id}:progress"
    payload = {
        "session_id": session_id,
        "progress_percent": percent,
        "status": status.value,
        "matched_items": matched,
        "total_items": total,
        "message": f"Обработано {matched} из {total} позиций",
    }
    redis_client.publish(channel, json.dumps(payload, ensure_ascii=False))


async def _process_session_in_db(session_id: int) -> None:
    """Обработка позиций сессии батчами с трансляцией прогресса."""
    async with WorkerSessionLocal() as db:
        try:
            stmt = select(ProcurementSession).where(ProcurementSession.id == session_id)
            result = await db.execute(stmt)
            session = result.scalar_one_or_none()

            if not session:
                logger.error("ProcurementSession с id=%s не найдена.", session_id)
                return

            session.status = TaskStatusEnum.PROCESSING
            await db.commit()
            logger.info("Сессия %s переведена в PROCESSING.", session_id)

            items_stmt = select(RawOfferItem).where(RawOfferItem.session_id == session_id)
            items_result = await db.execute(items_stmt)
            items = items_result.scalars().all()

            total_items = len(items)
            matched_count = 0

            publish_progress(
                session_id=session_id,
                percent=10 if total_items > 0 else 100,
                status=TaskStatusEnum.PROCESSING,
                matched=0,
                total=total_items,
            )

            for i in range(0, total_items, BATCH_SIZE):
                batch = items[i : i + BATCH_SIZE]

                await asyncio.sleep(0.3)

                matched_count += len(batch)
                session.matched_items = matched_count
                await db.commit()

                if total_items > 0:
                    percent = int(10 + (matched_count / total_items) * 85)
                    publish_progress(
                        session_id=session_id,
                        percent=percent,
                        status=TaskStatusEnum.PROCESSING,
                        matched=matched_count,
                        total=total_items,
                    )
                    logger.info(
                        "Сессия %s: батч %d/%d, прогресс %d%%.",
                        session_id,
                        i // BATCH_SIZE + 1,
                        -(-total_items // BATCH_SIZE),
                        percent,
                    )

            session.status = TaskStatusEnum.COMPLETED
            await db.commit()

            publish_progress(
                session_id=session_id,
                percent=100,
                status=TaskStatusEnum.COMPLETED,
                matched=matched_count,
                total=total_items,
            )
            logger.info("Сессия %s успешно завершена.", session_id)

        except Exception as exc:
            await db.rollback()
            logger.exception("Ошибка при обработке сессии %s: %s", session_id, exc)

            async with WorkerSessionLocal() as err_db:
                err_stmt = select(ProcurementSession).where(ProcurementSession.id == session_id)
                res = await err_db.execute(err_stmt)
                err_session = res.scalar_one_or_none()
                if err_session:
                    err_session.status = TaskStatusEnum.FAILED
                    await err_db.commit()

            publish_progress(
                session_id=session_id,
                percent=100,
                status=TaskStatusEnum.FAILED,
                matched=0,
                total=0,
            )
            raise exc


@celery_app.task(name="process_procurement_session", bind=True)
def process_procurement_session(self, session_id: int) -> dict:
    """Точка входа Celery для обработки прайс-листа."""
    logger.info("Старт фоновой таски для сессии %s", session_id)
    asyncio.run(_process_session_in_db(session_id))
    return {"status": "SUCCESS", "session_id": session_id}
