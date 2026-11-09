import asyncio
import json
import logging
import sys
from celery.signals import worker_process_init
import httpx
import redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.ml_worker.vector_search import get_embedding_model, search_top_candidates_batch
from src.server.core.celery_app import REDIS_URL, celery_app
from src.server.core.config import settings
from src.server.core.database import SQLALCHEMY_DATABASE_URL
from src.server.models.procurement import (
    MatchStatusEnum,
    ProcurementSession,
    RawOfferItem,
    TaskStatusEnum,
)
from src.server.models.tire import Tire

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


@worker_process_init.connect
def preload_ml_model(**kwargs: object) -> None:
    """Загружает Bi-Encoder один раз при старте процесса ML-воркера, а не на каждую таску."""
    argv_joined = " ".join(sys.argv)
    if "ml" in argv_joined:
        try:
            get_embedding_model()
            logger.info("ML-модель успешно предзагружена в процессе воркера.")
        except Exception as exc:
            logger.warning("Не удалось предзагрузить ML-модель при старте воркера: %s", exc)


def publish_progress(
    session_id: int,
    percent: int,
    status: TaskStatusEnum,
    matched: int,
    total: int,
    processed: int | None = None,
) -> None:
    """Публикация прогресса в Redis-канал session:{id}:progress."""
    done_count = processed if processed is not None else matched
    channel = f"session:{session_id}:progress"
    payload = {
        "session_id": session_id,
        "progress_percent": percent,
        "status": status.value,
        "matched_items": matched,
        "total_items": total,
        "message": f"Обработано {done_count} из {total} позиций",
    }
    redis_client.publish(channel, json.dumps(payload, ensure_ascii=False))


async def _match_batch_via_external_ai_server(
    enterprise_catalog: list[dict[str, object]],
    batch: list[RawOfferItem],
) -> int:
    """
    Межсервисный вызов внешнего 'Сервера, запускающего ИИ' по контракту 'Две таблицы + токен'
    (активируется, если задан EXTERNAL_AI_SERVER_URL). Возвращает число AUTO-сопоставлений в батче.
    """
    base_url = settings.EXTERNAL_AI_SERVER_URL.strip().rstrip("/")
    endpoint = f"{base_url}/api/v1/ai/match-tables"
    req_body = {
        "enterprise_table": enterprise_catalog,
        "supplier_table": [
            {
                "raw_name": item.raw_name,
                "price": float(item.price),
                "quantity": int(item.quantity),
                "supplier_article": item.supplier_article,
            }
            for item in batch
        ],
        "top_k": int(settings.MATCH_TOP_K),
    }
    headers = {"X-AI-Service-Token": settings.AI_SERVICE_TOKEN}

    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        resp = await client.post(endpoint, json=req_body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    matched_rows = data.get("items", [])
    auto_in_batch = 0
    for item, row in zip(batch, matched_rows):
        item.candidates = list(row.get("candidates") or [])
        item.confidence_score = float(row.get("confidence_score") or 0.0)
        status_str = str(row.get("match_status") or MatchStatusEnum.UNMATCHED.value)
        ent_id = row.get("enterprise_id")

        if status_str == MatchStatusEnum.AUTO.value and ent_id is not None:
            item.matched_tire_id = int(ent_id)
            item.match_status = MatchStatusEnum.AUTO
            auto_in_batch += 1
        elif status_str == MatchStatusEnum.NEEDS_REVIEW.value and ent_id is not None:
            item.matched_tire_id = int(ent_id)
            item.match_status = MatchStatusEnum.NEEDS_REVIEW
        else:
            item.matched_tire_id = None
            item.match_status = MatchStatusEnum.UNMATCHED

    return auto_in_batch


async def _process_session_in_db(
    session_id: int,
    session_factory: async_sessionmaker[AsyncSession] = WorkerSessionLocal,
) -> None:
    """Обработка позиций сессии батчами через Bi-Encoder + pgvector с трансляцией прогресса."""
    total_items = 0
    auto_matched_count = 0
    processed_count = 0
    batch_size = max(1, settings.MATCH_BATCH_SIZE)

    async with session_factory() as db:
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

            items_stmt = (
                select(RawOfferItem)
                .where(RawOfferItem.session_id == session_id)
                .order_by(RawOfferItem.id)
            )
            items_result = await db.execute(items_stmt)
            items = list(items_result.scalars().all())

            total_items = len(items)
            session.total_items = total_items

            publish_progress(
                session_id=session_id,
                percent=10 if total_items > 0 else 100,
                status=TaskStatusEnum.PROCESSING,
                matched=0,
                total=total_items,
                processed=0,
            )

            use_external_ai = bool(settings.EXTERNAL_AI_SERVER_URL.strip())
            enterprise_catalog: list[dict[str, object]] = []
            if use_external_ai:
                tires_res = await db.execute(select(Tire).order_by(Tire.id))
                enterprise_catalog = [
                    {
                        "id": t.id,
                        "sku": t.sku,
                        "search_text": t.search_text or t.sku,
                    }
                    for t in tires_res.scalars().all()
                ]

            for i in range(0, total_items, batch_size):
                batch = items[i : i + batch_size]

                if use_external_ai:
                    auto_matched_count += await _match_batch_via_external_ai_server(
                        enterprise_catalog=enterprise_catalog,
                        batch=batch,
                    )
                else:
                    batch_names = [item.raw_name for item in batch]

                    batch_candidates = await search_top_candidates_batch(
                        db=db,
                        names=batch_names,
                        top_k=settings.MATCH_TOP_K,
                    )

                    for item, candidates in zip(batch, batch_candidates):
                        item.candidates = [
                            {
                                "tire_id": tire.id,
                                "sku": tire.sku,
                                "score": score,
                            }
                            for tire, score in candidates
                        ]

                        if candidates:
                            best_tire, best_score = candidates[0]
                            item.confidence_score = best_score
                            if best_score >= settings.MATCH_AUTO_THRESHOLD:
                                item.matched_tire_id = best_tire.id
                                item.match_status = MatchStatusEnum.AUTO
                                auto_matched_count += 1
                            elif best_score >= settings.MATCH_REVIEW_THRESHOLD:
                                item.matched_tire_id = best_tire.id
                                item.match_status = MatchStatusEnum.NEEDS_REVIEW
                            else:
                                item.matched_tire_id = None
                                item.match_status = MatchStatusEnum.UNMATCHED
                        else:
                            item.matched_tire_id = None
                            item.confidence_score = 0.0
                            item.match_status = MatchStatusEnum.UNMATCHED

                processed_count += len(batch)
                session.matched_items = auto_matched_count
                await db.commit()

                if total_items > 0:
                    percent = int(10 + (processed_count / total_items) * 85)
                    publish_progress(
                        session_id=session_id,
                        percent=percent,
                        status=TaskStatusEnum.PROCESSING,
                        matched=auto_matched_count,
                        total=total_items,
                        processed=processed_count,
                    )
                    logger.info(
                        "Сессия %s: батч %d/%d, прогресс %d%% (AUTO: %d/%d).",
                        session_id,
                        i // batch_size + 1,
                        -(-total_items // batch_size),
                        percent,
                        auto_matched_count,
                        processed_count,
                    )

            session.status = TaskStatusEnum.COMPLETED
            session.matched_items = auto_matched_count
            await db.commit()

            publish_progress(
                session_id=session_id,
                percent=100,
                status=TaskStatusEnum.COMPLETED,
                matched=auto_matched_count,
                total=total_items,
                processed=processed_count,
            )
            logger.info("Сессия %s успешно завершена.", session_id)

        except Exception as exc:
            await db.rollback()
            logger.exception("Ошибка при обработке сессии %s: %s", session_id, exc)

            async with session_factory() as err_db:
                err_stmt = select(ProcurementSession).where(ProcurementSession.id == session_id)
                res = await err_db.execute(err_stmt)
                err_session = res.scalar_one_or_none()
                if err_session:
                    err_session.status = TaskStatusEnum.FAILED
                    err_session.matched_items = auto_matched_count
                    if total_items == 0:
                        total_items = err_session.total_items
                    await err_db.commit()

            publish_progress(
                session_id=session_id,
                percent=100,
                status=TaskStatusEnum.FAILED,
                matched=auto_matched_count,
                total=total_items,
                processed=processed_count,
            )
            raise


@celery_app.task(name="process_procurement_session", bind=True)
def process_procurement_session(self: object, session_id: int) -> dict[str, object]:
    """Точка входа Celery для фоновой обработки прайс-листа."""
    logger.info("Старт фоновой таски для сессии %s", session_id)
    asyncio.run(_process_session_in_db(session_id))
    return {"status": "SUCCESS", "session_id": session_id}
