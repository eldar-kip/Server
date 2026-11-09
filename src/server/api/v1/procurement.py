import csv
import io
from typing import Any, Literal
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
import pandas as pd
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.server.api.deps import ensure_session_access, get_current_user
from src.server.core.config import settings
from src.server.core.database import get_db
from src.server.models.procurement import (
    AuditActionEnum,
    DemandItem,
    FeedbackAuditLog,
    MatchStatusEnum,
    OptimizationResult,
    ProcurementSession,
    RawOfferItem,
    Supplier,
    TaskStatusEnum,
)
from src.server.models.tire import Tire
from src.server.models.user import RoleEnum, User
from src.server.schemas.ml_ops import MatchedNomenclatureRowSchema
from src.server.schemas.procurement import (
    AllocationItemSchema,
    ApproveResponse,
    DemandItemResponse,
    DemandUploadRequest,
    ManualMatchRequest,
    MatchItemResponse,
    OptimizationResponse,
    OptimizeRequest,
    ProcurementSessionDetail,
    ProcurementSessionResponse,
    SupplierCreate,
    SupplierResponse,
    UnfulfilledItemSchema,
    UploadProcurementResponse,
    WebhookExportRequest,
    WebhookExportResponse,
)
from src.server.services.erp_exporter import ERPExporterService
from src.server.services.file_parser import FileParserService
from src.server.services.split_optimizer import OfferCandidate, SplitOptimizerService
from src.server.tasks.erp_tasks import send_to_erp
from src.server.tasks.matching_tasks import process_procurement_session

router = APIRouter(prefix="/api/v1/procurement", tags=["Закупочные сессии"])


# ---------------------------------------------------------------------------
# Поставщики
# ---------------------------------------------------------------------------


@router.post("/suppliers", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
async def create_supplier(
    payload: SupplierCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Supplier:
    """Создание контрагента-поставщика для привязки прайс-листов и сплит-оптимизации."""
    if payload.inn:
        existing = await db.execute(select(Supplier).where(Supplier.inn == payload.inn))
        if existing.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Поставщик с таким ИНН уже зарегистрирован",
            )
    supplier = Supplier(name=payload.name.strip(), inn=payload.inn)
    db.add(supplier)
    await db.commit()
    await db.refresh(supplier)
    return supplier


@router.get("/suppliers", response_model=list[SupplierResponse])
async def list_suppliers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Supplier]:
    """Список всех зарегистрированных поставщиков."""
    res = await db.execute(select(Supplier).order_by(Supplier.id))
    return list(res.scalars().all())


# ---------------------------------------------------------------------------
# Создание и просмотр сессий (статические роуты ДО /{session_id})
# ---------------------------------------------------------------------------


@router.post("/upload", response_model=UploadProcurementResponse)
async def upload_procurement_file(
    file: UploadFile = File(...),
    supplier_id: int | None = Query(default=None, description="ID поставщика прайс-листа"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UploadProcurementResponse:
    """Прием файла прайс-листа (.xlsx/.csv), создание сессии и запуск фоновой ML-обработки."""
    if supplier_id is not None:
        sup_res = await db.execute(select(Supplier).where(Supplier.id == supplier_id))
        if sup_res.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail=f"Поставщик id={supplier_id} не найден")

    parsed_items_schemas = await FileParserService.parse_file(file)

    session_title = f"Сессия из файла: {file.filename}"
    new_session = ProcurementSession(
        user_id=current_user.id,
        title=session_title,
        status=TaskStatusEnum.PENDING,
        total_items=len(parsed_items_schemas),
        matched_items=0,
    )
    db.add(new_session)
    await db.commit()
    await db.refresh(new_session)

    raw_items = [
        RawOfferItem(
            session_id=new_session.id,
            supplier_id=supplier_id,
            raw_name=item.raw_name,
            price=item.price,
            quantity=item.quantity,
            lot_size=item.lot_size,
            min_order_qty=item.min_order_qty,
            supplier_article=item.supplier_article,
        )
        for item in parsed_items_schemas
    ]
    db.add_all(raw_items)
    await db.commit()

    process_procurement_session.delay(new_session.id)

    return UploadProcurementResponse(
        message="Файл успешно загружен и распарсен",
        session_id=new_session.id,
        total_items=len(raw_items),
    )


@router.get("/sessions", response_model=list[ProcurementSessionResponse])
@router.get("/sessions/", response_model=list[ProcurementSessionResponse], include_in_schema=False)
async def list_sessions(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ProcurementSession]:
    """Пагинированный список закупочных сессий (Buyer видит свои, Admin — все)."""
    stmt = select(ProcurementSession)
    if current_user.role != RoleEnum.ADMIN:
        stmt = stmt.where(ProcurementSession.user_id == current_user.id)
    stmt = stmt.offset(skip).limit(limit).order_by(ProcurementSession.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Ручная корректировка сопоставления закупщиком (TAI-106)
# ---------------------------------------------------------------------------


@router.patch("/items/{item_id}", response_model=MatchItemResponse)
async def manual_rematch_item(
    item_id: int,
    payload: ManualMatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RawOfferItem:
    """Ручная привязка другой эталонной шины закупщиком с записью в FeedbackAuditLog."""
    item_stmt = (
        select(RawOfferItem)
        .where(RawOfferItem.id == item_id)
        .options(selectinload(RawOfferItem.session))
    )
    res = await db.execute(item_stmt)
    item = res.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Позиция прайс-листа не найдена")

    session = item.session
    ensure_session_access(session, current_user)

    if session.status == TaskStatusEnum.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя изменять позиции уже утвержденной (APPROVED) сессии",
        )

    tire_res = await db.execute(select(Tire).where(Tire.id == payload.tire_id))
    tire = tire_res.scalar_one_or_none()
    if tire is None:
        raise HTTPException(status_code=404, detail=f"Шина с id={payload.tire_id} не найдена в каталоге")

    old_tire_id = item.matched_tire_id
    old_confidence = item.confidence_score

    item.matched_tire_id = tire.id
    item.match_status = MatchStatusEnum.MANUAL
    item.confidence_score = 1.0

    audit_entry = FeedbackAuditLog(
        session_id=session.id,
        item_id=item.id,
        user_id=current_user.id,
        action=AuditActionEnum.MANUAL_REMATCH,
        old_tire_id=old_tire_id,
        new_tire_id=tire.id,
        old_confidence=old_confidence,
    )
    db.add(audit_entry)
    await db.flush()

    # Пересчитываем число надежно сопоставленных позиций сессии (AUTO + MANUAL)
    count_stmt = (
        select(func.count(RawOfferItem.id))
        .where(RawOfferItem.session_id == session.id)
        .where(RawOfferItem.match_status.in_([MatchStatusEnum.AUTO, MatchStatusEnum.MANUAL]))
    )
    matched_res = await db.execute(count_stmt)
    session.matched_items = int(matched_res.scalar_one() or 0)

    await db.commit()

    refreshed_stmt = (
        select(RawOfferItem)
        .where(RawOfferItem.id == item.id)
        .options(selectinload(RawOfferItem.matched_tire))
    )
    refreshed = await db.execute(refreshed_stmt)
    return refreshed.scalar_one()


# ---------------------------------------------------------------------------
# Операции над конкретной сессией /{session_id}
# ---------------------------------------------------------------------------


async def _get_authorized_session(
    session_id: int,
    db: AsyncSession,
    current_user: User,
) -> ProcurementSession:
    stmt = select(ProcurementSession).where(ProcurementSession.id == session_id)
    res = await db.execute(stmt)
    session = res.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    ensure_session_access(session, current_user)
    return session


@router.post("/{session_id}/upload", response_model=UploadProcurementResponse)
async def upload_additional_supplier_file(
    session_id: int,
    file: UploadFile = File(...),
    supplier_id: int | None = Query(default=None, description="ID поставщика дополнительного прайс-листа"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UploadProcurementResponse:
    """Дозагрузка прайс-листа еще одного поставщика в существующую закупочную сессию."""
    session = await _get_authorized_session(session_id, db, current_user)
    if session.status == TaskStatusEnum.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя дозагружать файлы в уже утвержденную (APPROVED) сессию",
        )

    if supplier_id is not None:
        sup_res = await db.execute(select(Supplier).where(Supplier.id == supplier_id))
        if sup_res.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail=f"Поставщик id={supplier_id} не найден")

    parsed_items = await FileParserService.parse_file(file)
    new_rows = [
        RawOfferItem(
            session_id=session.id,
            supplier_id=supplier_id,
            raw_name=item.raw_name,
            price=item.price,
            quantity=item.quantity,
            lot_size=item.lot_size,
            min_order_qty=item.min_order_qty,
            supplier_article=item.supplier_article,
        )
        for item in parsed_items
    ]
    db.add_all(new_rows)
    session.total_items += len(new_rows)
    session.status = TaskStatusEnum.PENDING
    await db.commit()

    process_procurement_session.delay(session.id)

    return UploadProcurementResponse(
        message="Дополнительный прайс-лист загружен и поставлен в очередь",
        session_id=session.id,
        total_items=session.total_items,
    )


@router.get("/{session_id}", response_model=ProcurementSessionDetail)
async def get_session_details(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProcurementSession:
    """Получение статуса и полного списка позиций конкретной закупочной сессии."""
    stmt = (
        select(ProcurementSession)
        .where(ProcurementSession.id == session_id)
        .options(selectinload(ProcurementSession.raw_offer_items))
    )
    result = await db.execute(stmt)
    session = result.scalars().first()
    if not session:
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    ensure_session_access(session, current_user)
    return session


@router.get("/{session_id}/matches", response_model=list[MatchItemResponse])
async def get_session_matches(
    session_id: int,
    max_confidence: float = Query(
        0.7,
        ge=0.0,
        le=1.0,
        description="Фильтр позиций с Confidence Score строго меньше порога (или UNMATCHED)",
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[RawOfferItem]:
    """Список позиций сессии, требующих проверки закупщиком (Confidence Score < max_confidence или UNMATCHED)."""
    await _get_authorized_session(session_id, db, current_user)

    stmt = (
        select(RawOfferItem)
        .where(RawOfferItem.session_id == session_id)
        .where(
            or_(
                RawOfferItem.confidence_score.is_(None),
                RawOfferItem.confidence_score < max_confidence,
                RawOfferItem.match_status == MatchStatusEnum.UNMATCHED,
            )
        )
        .options(selectinload(RawOfferItem.matched_tire))
        .order_by(RawOfferItem.confidence_score.asc().nullsfirst(), RawOfferItem.id.asc())
        .offset(skip)
        .limit(limit)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.get("/{session_id}/matched-table")
async def get_session_matched_nomenclature_table(
    session_id: int,
    format: Literal["json", "csv", "xlsx"] = Query(
        "json",
        description="Формат возврата сопоставленной номенклатуры для Сайта / Модуля CRM",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Возвращает полную таблицу 'Возврат сопоставленной номенклатуры' для Сайта | Модуля CRM:
    Предприятие | Поставщик | Цена поставщика | Точность совпадения (JSON, CSV или XLSX).
    """
    await _get_authorized_session(session_id, db, current_user)

    stmt = (
        select(RawOfferItem)
        .where(RawOfferItem.session_id == session_id)
        .options(selectinload(RawOfferItem.matched_tire))
        .order_by(RawOfferItem.id.asc())
    )
    items = list((await db.execute(stmt)).scalars().all())

    rows: list[MatchedNomenclatureRowSchema] = []
    for idx, item in enumerate(items, start=1):
        cands = list(item.candidates or [])
        ent_name: str | None = None
        if item.matched_tire is not None:
            ent_name = item.matched_tire.sku
        elif cands and isinstance(cands[0], dict) and cands[0].get("sku"):
            ent_name = str(cands[0]["sku"])

        rows.append(
            MatchedNomenclatureRowSchema(
                row_number=idx,
                enterprise_id=item.matched_tire_id,
                enterprise_name=ent_name,
                supplier_name=item.raw_name,
                supplier_price=round(float(item.price), 2),
                confidence_score=round(float(item.confidence_score or 0.0), 4),
                match_status=item.match_status,
                candidates=cands,
            )
        )

    if format == "json":
        return rows

    tabular_records = [
        {
            "#": r.row_number,
            "Предприятие": r.enterprise_name or "",
            "Поставщик": r.supplier_name,
            "Цена поставщика": r.supplier_price,
            "Точность совпадения": r.confidence_score,
            "Статус": r.match_status.value,
        }
        for r in rows
    ]

    if format == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=["#", "Предприятие", "Поставщик", "Цена поставщика", "Точность совпадения", "Статус"],
            delimiter=";",
        )
        writer.writeheader()
        writer.writerows(tabular_records)
        headers = {
            "Content-Disposition": f'attachment; filename="matched_session_{session_id}.csv"',
        }
        return StreamingResponse(
            io.BytesIO(buf.getvalue().encode("utf-8-sig")),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    df = pd.DataFrame(tabular_records)
    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Сопоставление")
    excel_buf.seek(0)
    headers = {
        "Content-Disposition": f'attachment; filename="matched_session_{session_id}.xlsx"',
    }
    return StreamingResponse(
        excel_buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@router.post("/{session_id}/approve", response_model=ApproveResponse)
async def approve_session(
    session_id: int,
    force: bool = Query(
        False,
        description="Разрешить утверждение даже при наличии несопоставленных (UNMATCHED) позиций",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ApproveResponse:
    """Подтверждение закупщиком всех сопоставлений сессии (перевод из COMPLETED в APPROVED)."""
    session = await _get_authorized_session(session_id, db, current_user)

    if session.status != TaskStatusEnum.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Утверждение доступно только из статуса COMPLETED (текущий: {session.status.value})",
        )

    unmatched_stmt = (
        select(func.count(RawOfferItem.id))
        .where(RawOfferItem.session_id == session.id)
        .where(
            or_(
                RawOfferItem.matched_tire_id.is_(None),
                RawOfferItem.match_status == MatchStatusEnum.UNMATCHED,
            )
        )
    )
    unmatched_count = int((await db.execute(unmatched_stmt)).scalar_one() or 0)
    if unmatched_count > 0 and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"В сессии осталось {unmatched_count} несопоставленных позиций. "
                "Привяжите их вручную или передайте ?force=true."
            ),
        )

    session.status = TaskStatusEnum.APPROVED
    db.add(
        FeedbackAuditLog(
            session_id=session.id,
            item_id=None,
            user_id=current_user.id,
            action=AuditActionEnum.APPROVE,
        )
    )
    await db.commit()

    return ApproveResponse(
        session_id=session.id,
        status=session.status,
        matched_items=session.matched_items,
        total_items=session.total_items,
        message="Сопоставление позиций сессии успешно утверждено закупщиком",
    )


# ---------------------------------------------------------------------------
# Потребность и оптимизация закупки SplitOptimizer (TAI-105)
# ---------------------------------------------------------------------------


@router.post("/{session_id}/demand", response_model=list[DemandItemResponse])
async def set_session_demand(
    session_id: int,
    payload: DemandUploadRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DemandItem]:
    """Сохраняет (перезаписывает) список потребности закупщика для закупочной сессии."""
    session = await _get_authorized_session(session_id, db, current_user)

    # Агрегируем одинаковые tire_id
    aggregated: dict[int, int] = {}
    for item in payload.items:
        aggregated[item.tire_id] = aggregated.get(item.tire_id, 0) + item.required_qty

    tire_res = await db.execute(select(Tire.id).where(Tire.id.in_(aggregated.keys())))
    found_ids = set(tire_res.scalars().all())
    missing = sorted(set(aggregated.keys()) - found_ids)
    if missing:
        raise HTTPException(status_code=404, detail=f"Шины не найдены в каталоге: {missing}")

    await db.execute(delete(DemandItem).where(DemandItem.session_id == session.id))

    created = [
        DemandItem(session_id=session.id, tire_id=tid, required_qty=qty)
        for tid, qty in sorted(aggregated.items())
    ]
    db.add_all(created)
    await db.commit()
    for d in created:
        await db.refresh(d)
    return created


@router.post("/{session_id}/optimize", response_model=OptimizationResponse)
async def optimize_session_procurement(
    session_id: int,
    payload: OptimizeRequest = Body(default_factory=OptimizeRequest),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OptimizationResponse:
    """Запускает алгоритм SplitOptimizer для утвержденной (APPROVED) сессии и сохраняет план закупки."""
    session = await _get_authorized_session(session_id, db, current_user)
    if session.status != TaskStatusEnum.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Оптимизация доступна только для утвержденных (APPROVED) сессий (текущий: {session.status.value})",
        )

    # 1. Определяем потребность (из тела запроса или сохраненных DemandItem)
    demands: dict[int, int] = {}
    if payload.demand:
        for d in payload.demand:
            demands[d.tire_id] = demands.get(d.tire_id, 0) + d.required_qty
        await db.execute(delete(DemandItem).where(DemandItem.session_id == session.id))
        db.add_all(
            [
                DemandItem(session_id=session.id, tire_id=tid, required_qty=qty)
                for tid, qty in sorted(demands.items())
            ]
        )
        await db.flush()
    else:
        demand_res = await db.execute(
            select(DemandItem).where(DemandItem.session_id == session.id)
        )
        for d_row in demand_res.scalars().all():
            demands[d_row.tire_id] = demands.get(d_row.tire_id, 0) + d_row.required_qty

    if not demands:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не задана потребность для закупки (передайте поле 'demand' или вызовите POST /{session_id}/demand).",
        )

    # 2. Загружаем все сопоставленные позиции прайс-листов сессии
    offers_stmt = (
        select(RawOfferItem)
        .where(RawOfferItem.session_id == session.id)
        .where(RawOfferItem.matched_tire_id.is_not(None))
        .options(
            selectinload(RawOfferItem.matched_tire),
            selectinload(RawOfferItem.supplier),
        )
    )
    offers_res = await db.execute(offers_stmt)
    raw_offers = offers_res.scalars().all()

    candidates = [
        OfferCandidate(
            offer_item_id=row.id,
            tire_id=row.matched_tire_id,  # type: ignore[arg-type]
            sku=row.matched_tire.sku if row.matched_tire else None,
            price=row.price,
            stock_qty=row.quantity,
            lot_size=row.lot_size,
            min_order_qty=row.min_order_qty,
            supplier_id=row.supplier_id,
            supplier_name=row.supplier.name if row.supplier else None,
            supplier_article=row.supplier_article,
        )
        for row in raw_offers
        if row.matched_tire_id is not None
    ]

    result = SplitOptimizerService.optimize(
        demands=demands,
        offers=candidates,
        min_supplier_order_amount=payload.min_supplier_order_amount,
        timeout_seconds=payload.timeout_seconds,
    )

    # 3. Сохраняем (обновляем) OptimizationResult для последующего экспорта в 1С / ERP
    existing_opt_res = await db.execute(
        select(OptimizationResult).where(OptimizationResult.session_id == session.id)
    )
    opt_model = existing_opt_res.scalar_one_or_none()
    if opt_model is None:
        opt_model = OptimizationResult(
            session_id=session.id,
            total_cost=result.total_cost,
            baseline_cost=result.baseline_cost,
            savings=result.savings,
            savings_percent=result.savings_percent,
            solver_method=result.solver_method,
            allocations=result.allocations,
            unfulfilled=result.unfulfilled,
        )
        db.add(opt_model)
    else:
        opt_model.total_cost = result.total_cost
        opt_model.baseline_cost = result.baseline_cost
        opt_model.savings = result.savings
        opt_model.savings_percent = result.savings_percent
        opt_model.solver_method = result.solver_method
        opt_model.allocations = result.allocations
        opt_model.unfulfilled = result.unfulfilled

    await db.commit()

    return OptimizationResponse(
        session_id=session.id,
        solver_method=result.solver_method,
        allocations=[AllocationItemSchema(**a) for a in result.allocations],
        total_cost=result.total_cost,
        baseline_cost=result.baseline_cost,
        savings=result.savings,
        savings_percent=result.savings_percent,
        unfulfilled=[UnfulfilledItemSchema(**u) for u in result.unfulfilled],
    )


# ---------------------------------------------------------------------------
# Экспорт спецификации в 1С / ERP (TAI-107)
# ---------------------------------------------------------------------------


@router.get("/{session_id}/export/1c")
async def export_session_1c(
    session_id: int,
    format: Literal["xml", "json"] = Query("xml", description="Формат выгрузки: CommerceML XML или JSON"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Скачивание файла закупочной спецификации в формате 1С CommerceML 2.08 (XML / JSON)."""
    session = await _get_authorized_session(session_id, db, current_user)
    if session.status != TaskStatusEnum.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Экспорт доступен только для утвержденных (APPROVED) сессий",
        )

    opt_res = await db.execute(
        select(OptimizationResult).where(OptimizationResult.session_id == session.id)
    )
    opt_model = opt_res.scalar_one_or_none()
    if opt_model is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Для сессии еще не выполнен расчет оптимизации (вызовите POST /{session_id}/optimize)",
        )

    sup_ids = {
        int(a["supplier_id"])
        for a in opt_model.allocations
        if a.get("supplier_id") is not None
    }
    suppliers_by_id: dict[int, Supplier] = {}
    if sup_ids:
        sup_rows = await db.execute(select(Supplier).where(Supplier.id.in_(sup_ids)))
        suppliers_by_id = {s.id: s for s in sup_rows.scalars().all()}

    if format == "xml":
        content_bytes = ERPExporterService.generate_commerceml_xml(
            session=session,
            opt_result=opt_model,
            suppliers_by_id=suppliers_by_id,
        )
        media_type = "application/xml"
        ext = "xml"
    else:
        content_bytes = ERPExporterService.generate_json_bytes(
            session=session,
            opt_result=opt_model,
            suppliers_by_id=suppliers_by_id,
        )
        media_type = "application/json"
        ext = "json"

    headers = {
        "Content-Disposition": f'attachment; filename="session_{session.id}.{ext}"',
    }
    return StreamingResponse(io.BytesIO(content_bytes), media_type=media_type, headers=headers)


@router.post("/{session_id}/export/webhook", response_model=WebhookExportResponse)
async def export_session_webhook(
    session_id: int,
    payload: WebhookExportRequest = Body(default_factory=WebhookExportRequest),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WebhookExportResponse:
    """Постановка фоновой задачи Celery для отправки заказов поставщикам во внешнюю ERP по Webhook."""
    session = await _get_authorized_session(session_id, db, current_user)
    if session.status != TaskStatusEnum.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Отправка в ERP доступна только для утвержденных (APPROVED) сессий",
        )

    opt_res = await db.execute(
        select(OptimizationResult).where(OptimizationResult.session_id == session.id)
    )
    if opt_res.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Для сессии еще не выполнен расчет оптимизации (вызовите POST /{session_id}/optimize)",
        )

    target_url = (payload.webhook_url or settings.ERP_WEBHOOK_URL).strip()
    if not target_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не указан URL вебхука ERP (задайте ERP_WEBHOOK_URL в окружении или передайте webhook_url в теле запроса)",
        )

    async_result = send_to_erp.delay(session.id, target_url)
    return WebhookExportResponse(
        session_id=session.id,
        task_id=str(async_result.id),
        target_url=target_url,
        status="QUEUED",
    )
