import enum
from datetime import datetime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import String, Integer, ForeignKey, Enum, DateTime, Float, func
from .base import Base

# 1. Определение TaskStatusEnum
class TaskStatusEnum(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

# 2. Модель ProcurementSession
class ProcurementSession(Base):
    __tablename__ = "procurement_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[TaskStatusEnum] = mapped_column(Enum(TaskStatusEnum), default=TaskStatusEnum.PENDING)
    total_items: Mapped[int] = mapped_column(default=0)
    matched_items: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())  # noqa: F821
    # 3. Настройка каскадного удаления cascade="all, delete-orphan"
    raw_offer_items: Mapped[list["RawOfferItem"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan"
    )

# 4. Модель RawOfferItem
class RawOfferItem(Base):
    __tablename__ = "raw_offer_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    
    # 5. Добавление внешнего ключа с ondelete="CASCADE"
    session_id: Mapped[int] = mapped_column(ForeignKey("procurement_sessions.id", ondelete="CASCADE"))
    
    raw_name: Mapped[str] = mapped_column(String(255))
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer)
    supplier_article: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Обратная связь с сессией
    session: Mapped["ProcurementSession"] = relationship(back_populates="raw_offer_items")