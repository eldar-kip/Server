from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from src.server.models.ml_training import TrainingStatusEnum
from src.server.models.procurement import MatchStatusEnum


class DatasetPairSchema(BaseModel):
    """Обучающая пара (сырое наименование поставщика -> эталон предприятия)."""

    query: str = Field(..., min_length=2, description="Сырое наименование из прайса поставщика")
    positive_passage: str = Field(..., min_length=2, description="Эталонное описание шины предприятия")


class TrainingJobCreate(BaseModel):
    """
    Входные параметры для запуска обучения в контуре tAIres-Trainer
    (соответствует всем 8 полям блока 'Ввод' архитектурной схемы).
    """

    model_name: str = Field("taires-bi-encoder", min_length=2, max_length=120, description="Название модели")
    model_version: str = Field("1.0.0", min_length=1, max_length=60, description="Версия модели")
    base_model: str = Field(
        "intfloat/multilingual-e5-large",
        min_length=2,
        max_length=255,
        description="Предобученная модель (с HuggingFace, локальная или s3://...)",
    )
    batch_size: int = Field(16, ge=1, le=256, description="Размер батча")
    epochs: int = Field(3, ge=1, le=50, description="Количество эпох")
    learning_rate: float = Field(2e-5, gt=0.0, le=1e-1, description="Learning rate")
    val_split: float = Field(
        0.2,
        ge=0.05,
        le=0.5,
        description="Доля данных, уходящая для валидации и автотеста (0.05..0.50)",
    )
    dataset: Optional[list[DatasetPairSchema]] = Field(
        default=None,
        description="Явно переданный датасет пар (если не указан, собирается из FeedbackAuditLog и каталога)",
    )
    use_feedback_log: bool = Field(
        default=True,
        description="Дополнять датасет накопленными исправлениями закупщиков из FeedbackAuditLog",
    )


class TrainingJobResponse(BaseModel):
    """
    Состояние задачи tAIres-Trainer и все выходные артефакты блока 'Вывод':
    телеметрия, точность автотеста, ссылка на ИИ-модель в S3 и метадата.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    model_name: str
    model_version: str
    base_model: str
    batch_size: int
    epochs: int
    learning_rate: float
    val_split: float
    dataset_size: int
    status: TrainingStatusEnum
    s3_uri: Optional[str] = None
    autotest_accuracy: Optional[float] = None
    telemetry: Optional[list[dict[str, Any]]] = None
    model_metadata: Optional[dict[str, Any]] = None
    is_active: bool = False
    error_message: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


class ModelActivateResponse(BaseModel):
    """Ответ при переключении активной ИИ-модели на Сервере, запускающем ИИ."""

    job_id: int
    model_name: str
    model_version: str
    active_model_uri: str
    message: str


class EnterpriseTableItemSchema(BaseModel):
    """Строка таблицы номенклатуры предприятия в контракте 'Две таблицы + токен'."""

    id: Optional[int] = None
    sku: str = Field(..., min_length=1, description="Наименование / SKU шины предприятия")
    search_text: Optional[str] = Field(None, description="Опциональное развернутое описание шины")


class SupplierTableItemSchema(BaseModel):
    """Строка таблицы поставщика в контракте 'Две таблицы + токен'."""

    raw_name: str = Field(..., min_length=1, description="Наименование товара у поставщика")
    price: float = Field(..., gt=0, description="Цена поставщика")
    quantity: int = Field(1, ge=1, description="Количество у поставщика")
    supplier_article: Optional[str] = None


class TwoTablesMatchRequest(BaseModel):
    """Запрос межсервисного сопоставления 'Две таблицы + токен'."""

    enterprise_table: list[EnterpriseTableItemSchema] = Field(
        ...,
        description="Таблица эталонной номенклатуры предприятия",
    )
    supplier_table: list[SupplierTableItemSchema] = Field(
        ...,
        min_length=1,
        description="Таблица прайс-листа поставщика",
    )
    top_k: int = Field(5, ge=1, le=50, description="Количество лучших кандидатов")


class MatchedNomenclatureRowSchema(BaseModel):
    """Строка результирующей таблицы 'Возврат сопоставленной номенклатуры' для Сайта / CRM."""

    row_number: int
    enterprise_id: Optional[int] = None
    enterprise_name: Optional[str] = Field(None, description="Номенклатура предприятия (Шина_1)")
    supplier_name: str = Field(..., description="Наименование поставщика (ШиНа1)")
    supplier_price: float = Field(..., description="Цена поставщика")
    confidence_score: float = Field(..., description="Точность совпадения (0..1)")
    match_status: MatchStatusEnum
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class TwoTablesMatchResponse(BaseModel):
    """Ответ 'Сервера, запускающего ИИ' со списком сопоставленной номенклатуры."""

    active_model: str
    total_rows: int
    items: list[MatchedNomenclatureRowSchema]
