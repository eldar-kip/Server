import asyncio
import logging
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from src.server.core.celery_app import celery_app
from src.server.core.database import SQLALCHEMY_DATABASE_URL
from src.server.models.procurement import OptimizationResult, ProcurementSession, Supplier
from src.server.services.erp_exporter import ERPExporterService

logger = logging.getLogger("celery.erp")

erp_engine = create_async_engine(SQLALCHEMY_DATABASE_URL, poolclass=NullPool)
ErpSessionLocal = async_sessionmaker(
    bind=erp_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def _export_session_to_erp_async(
    session_id: int,
    target_url: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] = ErpSessionLocal,
) -> dict[str, object]:
    """Читает утвержденную сессию и результат оптимизации из БД и отправляет по Webhook."""
    async with session_factory() as db:
        stmt = (
            select(ProcurementSession)
            .where(ProcurementSession.id == session_id)
            .options(selectinload(ProcurementSession.optimization_result))
        )
        res = await db.execute(stmt)
        session = res.scalar_one_or_none()
        if session is None:
            raise ValueError(f"Сессия закупки id={session_id} не найдена.")

        opt_result: OptimizationResult | None = session.optimization_result
        if opt_result is None:
            raise ValueError(f"Для сессии id={session_id} отсутствует результат оптимизации.")

        sup_ids = {
            int(a["supplier_id"])
            for a in opt_result.allocations
            if a.get("supplier_id") is not None
        }
        suppliers_by_id: dict[int, Supplier] = {}
        if sup_ids:
            sup_res = await db.execute(select(Supplier).where(Supplier.id.in_(sup_ids)))
            suppliers_by_id = {s.id: s for s in sup_res.scalars().all()}

        payload = ERPExporterService.build_export_data(
            session=session,
            opt_result=opt_result,
            suppliers_by_id=suppliers_by_id,
        )
        delivery = await ERPExporterService.send_webhook(payload=payload, target_url=target_url)
        logger.info(
            "Спецификация сессии %s успешно отправлена в ERP (%s), HTTP %s.",
            session_id,
            delivery["target_url"],
            delivery["status_code"],
        )
        return delivery


@celery_app.task(
    name="send_to_erp",
    bind=True,
    autoretry_for=(httpx.HTTPError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_to_erp(self: object, session_id: int, target_url: str | None = None) -> dict[str, object]:
    """Фоновая задача Celery отправки заказа поставщикам во внешнюю ERP с экспоненциальным ретраем."""
    return asyncio.run(_export_session_to_erp_async(session_id=session_id, target_url=target_url))
