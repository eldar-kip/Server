from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.server.core.database import get_db
from src.server.models.procurement import ProcurementSession, RawOfferItem, TaskStatusEnum
from src.server.services.file_parser import FileParserService

router = APIRouter(prefix="/api/v1/procurement", tags=["Закупочные сессии"])

@router.post("/upload")
async def upload_procurement_file(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """ 
    Прием UploadFile, парсинг строк и сохранение сессии в БД.
    """
    # Вызов парсера файлов (подзадача 2 и 5)
    parsed_items_schemas = await FileParserService.parse_file(file)

    # Создание родительской ProcurementSession
    session_title = f"Сессия из файла: {file.filename}"
    new_session = ProcurementSession(
        title=session_title,
        status=TaskStatusEnum.PENDING,
        total_items=len(parsed_items_schemas),
        matched_items=0
    )
    db.add(new_session)
    await db.commit()
    await db.refresh(new_session)

    # Сохранение распарсенных товаров (RawOfferItem)
    raw_items = [
        RawOfferItem(
            session_id=new_session.id,
            raw_name=item.raw_name,
            price=item.price,
            quantity=item.quantity,
            supplier_article=item.supplier_article
        )
        for item in parsed_items_schemas
    ]
    db.add_all(raw_items)
    await db.commit()

    return {
        "message": "Файл успешно загружен и распарсен", 
        "session_id": new_session.id, 
        "total_items": len(raw_items)
    }

@router.get("/{session_id}")
async def get_session_details(session_id: int, db: AsyncSession = Depends(get_db)):
    """
    Получение статуса и списка распарсенных товаров конкретной сессии (подзадача 3).
    """
    # Используем selectinload для асинхронной подгрузки связанных сырых позиций
    stmt = select(ProcurementSession).where(
        ProcurementSession.id == session_id
    ).options(selectinload(ProcurementSession.raw_offer_items))
    
    result = await db.execute(stmt)
    session = result.scalars().first()

    if not session:
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    return session

@router.get("/sessions/")
async def list_sessions(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db)
):
    """
    Пагинированный список всех сессий закупщика (подзадача 4).
    """
    stmt = select(ProcurementSession).offset(skip).limit(limit).order_by(ProcurementSession.created_at.desc())
    result = await db.execute(stmt)
    sessions = result.scalars().all()

    return sessions