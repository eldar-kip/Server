from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

# Схема для отдельной позиций прайс-листа
class RawOfferItemSchema(BaseModel):
    id: Optional[int] = None
    raw_name: str = Field(..., description="Исходное наименование шины из прайс-листа")
    price: float = Field(..., gt=0, description="Цена за единицу")
    quantity: int = Field(..., ge=1, description="Количество в наличии")
    supplier_article: Optional[str] = Field(None, description="Артикул поставщика")

    class Config:
        from_attributes = True

# Схема для создания закупочной сессии (Request)
class ProcurementSessionCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=255, description="Название закупочной сессии")
    raw_items: list[RawOfferItemSchema] = Field(..., description="Список позиций из загруженного файла")

# Схема ответа закупочной сессии (Response)
class ProcurementSessionResponse(BaseModel):
    id: int
    title: str
    status: TaskStatus
    total_items: int
    matched_items: int = 0
    created_at: datetime

    class Config:
        from_attributes = True
# Схема для возврата статуса обработки с прогрессом (Пункт 5)
class ProcurementStatusResponse(BaseModel):
    id: int
    status: TaskStatus
    total_items: int
    matched_items: int
    progress_percent: float = Field(..., ge=0, le=100, description="Процент выполнения сопоставления")

    class Config:
        from_attributes = True