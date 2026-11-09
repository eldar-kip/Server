import enum
from datetime import datetime
from typing import Any
from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TrainingStatusEnum(str, enum.Enum):
    """Статусы задачи обучения нейросети в контуре tAIres-Trainer."""

    PENDING = "PENDING"
    TRAINING = "TRAINING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MLTrainingJob(Base):
    """
    Задача обучения / дообучения модели в tAIres-Trainer и реестр версий моделей в S3.
    Хранит все входные гиперпараметры, телеметрию по эпохам, точность автотеста и метадату.
    """

    __tablename__ = "ml_training_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Входные параметры обучения (блок "Ввод" архитектурной схемы)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(60), nullable=False)
    base_model: Mapped[str] = mapped_column(String(255), nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, default=16, nullable=False)
    epochs: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    learning_rate: Mapped[float] = mapped_column(Float, default=2e-5, nullable=False)
    val_split: Mapped[float] = mapped_column(Float, default=0.2, nullable=False)
    dataset_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Выходные артефакты (блок "Вывод" -> S3 с нейросетями)
    status: Mapped[TrainingStatusEnum] = mapped_column(
        Enum(TrainingStatusEnum),
        default=TrainingStatusEnum.PENDING,
        nullable=False,
    )
    s3_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    autotest_accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    telemetry: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    )
    model_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
        index=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
