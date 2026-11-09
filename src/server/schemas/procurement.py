from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from src.server.models.procurement import MatchStatusEnum, TaskStatusEnum

# Единый алиас статуса для обратной совместимости импортов
TaskStatus = TaskStatusEnum


class TireShortResponse(BaseModel):
    """Краткая карточка эталонной шины из каталога."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    width_mm: int
    profile_ratio: int
    diameter_inch: int
    load_index_code: Optional[int] = None
    speed_index_code: Optional[str] = None
    search_text: Optional[str] = None


class SupplierCreate(BaseModel):
    """Запрос на создание поставщика."""

    name: str = Field(..., min_length=2, max_length=255, description="Наименование поставщика")
    inn: Optional[str] = Field(None, max_length=20, description="ИНН контрагента")


class SupplierResponse(BaseModel):
    """Ответ с данными поставщика."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    inn: Optional[str] = None


class RawOfferItemSchema(BaseModel):
    """Схема для отдельной позиции прайс-листа."""

    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    raw_name: str = Field(..., description="Исходное наименование шины из прайс-листа")
    price: float = Field(..., gt=0, description="Цена за единицу")
    quantity: int = Field(..., ge=1, description="Количество в наличии")
    lot_size: int = Field(1, ge=1, description="Квант (кратность) поставки")
    min_order_qty: int = Field(1, ge=1, description="Минимальная партия заказа позиции")
    supplier_article: Optional[str] = Field(None, description="Артикул поставщика")
    supplier_id: Optional[int] = Field(None, description="ID поставщика")
    matched_tire_id: Optional[int] = None
    confidence_score: Optional[float] = None
    match_status: MatchStatusEnum = MatchStatusEnum.UNMATCHED
    candidates: Optional[list[dict[str, Any]]] = None


class ProcurementSessionCreate(BaseModel):
    """Схема для создания закупочной сессии из JSON."""

    title: str = Field(..., min_length=3, max_length=255, description="Название закупочной сессии")
    raw_items: list[RawOfferItemSchema] = Field(..., description="Список позиций прайс-листа")


class UploadProcurementResponse(BaseModel):
    """Ответ при загрузке прайс-листа."""

    message: str
    session_id: int
    total_items: int


class ProcurementSessionResponse(BaseModel):
    """Краткая схема ответа закупочной сессии."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    title: str
    status: TaskStatusEnum
    total_items: int
    matched_items: int = 0
    created_at: datetime


class ProcurementSessionDetail(ProcurementSessionResponse):
    """Детальная схема закупочной сессии со списком позиций."""

    raw_offer_items: list[RawOfferItemSchema] = Field(default_factory=list)


class ProcurementStatusResponse(BaseModel):
    """Статус обработки сессии с процентом прогресса."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: TaskStatusEnum
    total_items: int
    matched_items: int
    progress_percent: float = Field(..., ge=0, le=100, description="Процент выполнения сопоставления")


class MatchItemResponse(BaseModel):
    """Позиция прайс-листа с результатом ML-сопоставления и списком кандидатов."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    raw_name: str
    price: float
    quantity: int
    lot_size: int = 1
    min_order_qty: int = 1
    supplier_article: Optional[str] = None
    supplier_id: Optional[int] = None
    matched_tire_id: Optional[int] = None
    matched_tire: Optional[TireShortResponse] = None
    confidence_score: Optional[float] = None
    match_status: MatchStatusEnum
    candidates: Optional[list[dict[str, Any]]] = None


class ManualMatchRequest(BaseModel):
    """Запрос закупщика на ручную привязку эталонной шины."""

    tire_id: int = Field(..., gt=0, description="ID эталонной шины из каталога")


class ApproveResponse(BaseModel):
    """Ответ после утверждения результатов сопоставления сессии."""

    session_id: int
    status: TaskStatusEnum
    matched_items: int
    total_items: int
    message: str


class DemandItemCreate(BaseModel):
    """Позиция потребности в закупке."""

    tire_id: int = Field(..., gt=0, description="ID требуемой шины из каталога")
    required_qty: int = Field(..., ge=1, description="Требуемое количество штук")


class DemandItemResponse(BaseModel):
    """Сохраненная позиция потребности."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    tire_id: int
    required_qty: int


class DemandUploadRequest(BaseModel):
    """Пакетная загрузка потребности для сессии."""

    items: list[DemandItemCreate] = Field(..., min_length=1)


class OptimizeRequest(BaseModel):
    """Параметры запуска оптимизации сплита заказа."""

    demand: Optional[list[DemandItemCreate]] = Field(
        None,
        description="Список потребности (если не передан, берется из сохраненных DemandItem сессии)",
    )
    min_supplier_order_amount: float = Field(
        0.0,
        ge=0.0,
        description="Опциональная минимальная сумма заказа на одного поставщика",
    )
    timeout_seconds: float = Field(
        10.0,
        gt=0.0,
        le=120.0,
        description="Таймаут MILP-решателя в секундах",
    )


class AllocationItemSchema(BaseModel):
    """Строка оптимального распределения заказа конкретному поставщику."""

    tire_id: int
    sku: Optional[str] = None
    offer_item_id: int
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    supplier_article: Optional[str] = None
    qty: int
    lots: int
    lot_size: int
    price: float
    total: float


class UnfulfilledItemSchema(BaseModel):
    """Непокрытая потребность при нехватке остатков у поставщиков."""

    tire_id: int
    required_qty: int
    allocated_qty: int
    shortage_qty: int


class OptimizationResponse(BaseModel):
    """Итоговый план закупки и расчет экономии SplitOptimizer."""

    session_id: int
    solver_method: str
    allocations: list[AllocationItemSchema]
    total_cost: float
    baseline_cost: Optional[float] = None
    savings: float
    savings_percent: float
    unfulfilled: list[UnfulfilledItemSchema] = Field(default_factory=list)


class WebhookExportRequest(BaseModel):
    """Опциональные параметры отправки спецификации во внешнюю ERP."""

    webhook_url: Optional[str] = Field(
        None,
        description="URL вебхука ERP (по умолчанию используется ERP_WEBHOOK_URL из настроек)",
    )


class WebhookExportResponse(BaseModel):
    """Ответ на постановку задачи отправки в ERP по Webhook."""

    session_id: int
    task_id: str
    target_url: str
    status: str
