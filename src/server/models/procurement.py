import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any
from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, JSON, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .tire import Tire
    from .user import User


class TaskStatusEnum(str, enum.Enum):
    """Статусы жизненного цикла закупочной сессии."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    APPROVED = "APPROVED"


class MatchStatusEnum(str, enum.Enum):
    """Статус сопоставления конкретной позиции прайс-листа."""

    AUTO = "AUTO"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    MANUAL = "MANUAL"
    UNMATCHED = "UNMATCHED"


class AuditActionEnum(str, enum.Enum):
    """Типы действий закупщика для журналирования обратной связи (дообучение ML)."""

    MANUAL_REMATCH = "MANUAL_REMATCH"
    APPROVE = "APPROVE"


class Supplier(Base):
    """Поставщик шин (контрагент для сплит-оптимизации и выгрузки в 1С/ERP)."""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    inn: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)

    offer_items: Mapped[list["RawOfferItem"]] = relationship(back_populates="supplier")


class ProcurementSession(Base):
    """Закупочная сессия обработки прайс-листов поставщиков."""

    __tablename__ = "procurement_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[TaskStatusEnum] = mapped_column(
        Enum(TaskStatusEnum),
        default=TaskStatusEnum.PENDING,
    )
    total_items: Mapped[int] = mapped_column(default=0)
    matched_items: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    user: Mapped["User | None"] = relationship()
    raw_offer_items: Mapped[list["RawOfferItem"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    demand_items: Mapped[list["DemandItem"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    audit_logs: Mapped[list["FeedbackAuditLog"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    optimization_result: Mapped["OptimizationResult | None"] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        uselist=False,
    )


class RawOfferItem(Base):
    """Строка прайс-листа поставщика и результат ее сопоставления с каталогом."""

    __tablename__ = "raw_offer_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("procurement_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("suppliers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    raw_name: Mapped[str] = mapped_column(String(255))
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer)
    lot_size: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    min_order_qty: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    supplier_article: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Поля результата сопоставления с эталонным каталогом Tire
    matched_tire_id: Mapped[int | None] = mapped_column(
        ForeignKey("tires.id", ondelete="SET NULL"),
        nullable=True,
    )
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    match_status: Mapped[MatchStatusEnum] = mapped_column(
        Enum(MatchStatusEnum),
        default=MatchStatusEnum.UNMATCHED,
        server_default="UNMATCHED",
        nullable=False,
    )
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)

    session: Mapped["ProcurementSession"] = relationship(back_populates="raw_offer_items")
    supplier: Mapped["Supplier | None"] = relationship(back_populates="offer_items")
    matched_tire: Mapped["Tire | None"] = relationship()


class FeedbackAuditLog(Base):
    """Журнал действий закупщика по ручной корректировке и подтверждению сопоставлений."""

    __tablename__ = "feedback_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("procurement_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_offer_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[AuditActionEnum] = mapped_column(Enum(AuditActionEnum), nullable=False)
    old_tire_id: Mapped[int | None] = mapped_column(
        ForeignKey("tires.id", ondelete="SET NULL"),
        nullable=True,
    )
    new_tire_id: Mapped[int | None] = mapped_column(
        ForeignKey("tires.id", ondelete="SET NULL"),
        nullable=True,
    )
    old_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    session: Mapped["ProcurementSession"] = relationship(back_populates="audit_logs")


class DemandItem(Base):
    """Потребность в закупке конкретной шины в рамках закупочной сессии."""

    __tablename__ = "demand_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("procurement_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    tire_id: Mapped[int] = mapped_column(
        ForeignKey("tires.id", ondelete="CASCADE"),
        index=True,
    )
    required_qty: Mapped[int] = mapped_column(Integer, nullable=False)

    session: Mapped["ProcurementSession"] = relationship(back_populates="demand_items")
    tire: Mapped["Tire"] = relationship()


class OptimizationResult(Base):
    """Сохраненный результат распределения заказа между поставщиками (SplitOptimizer)."""

    __tablename__ = "optimization_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("procurement_sessions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    total_cost: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    savings: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    savings_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    solver_method: Mapped[str] = mapped_column(String(50), nullable=False)
    allocations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    )
    unfulfilled: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    session: Mapped["ProcurementSession"] = relationship(back_populates="optimization_result")
