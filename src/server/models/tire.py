from typing import TYPE_CHECKING, Optional
from sqlalchemy import String, Integer, ForeignKey, Table, Column
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
if TYPE_CHECKING:  
    from .references import (
        Technology,
        ConstructionType,
        LoadIndex,
        SpeedIndex,
        Seasonality,
        TreadPattern,
        Pattern
    )

# Промежуточная таблица для связи "Шины <-> Технологии"
tire_technologies = Table(
    "tire_technologies",
    Base.metadata,
    Column("tire_id", Integer, ForeignKey("tires.id", ondelete="CASCADE"), primary_key=True),
    Column("technology_id", Integer, ForeignKey("technologies.id", ondelete="CASCADE"), primary_key=True),
)

class Tire(Base):
    __tablename__ = "tires"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(100), unique=True)
    
    # Геометрические параметры
    width_mm: Mapped[int]
    profile_ratio: Mapped[int]
    diameter_inch: Mapped[int]
    
    # Внешние ключи (Foreign Keys)
    construction_type_code: Mapped[Optional[str]] = mapped_column(ForeignKey("construction_types.code"))
    load_index_code: Mapped[Optional[int]] = mapped_column(ForeignKey("load_indices.code"))
    speed_index_code: Mapped[Optional[str]] = mapped_column(ForeignKey("speed_indices.code"))
    seasonality_id: Mapped[Optional[int]] = mapped_column(ForeignKey("seasonalities.id"))
    tread_pattern_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tread_patterns.id"))
    pattern_id: Mapped[Optional[int]] = mapped_column(ForeignKey("patterns.id"))    

    # Связи (Relationships) — все зависимости указаны СТРОКАМИ
    # Связи (Relationships)
    technologies: Mapped[list["Technology"]] = relationship(secondary=tire_technologies)
    construction_type: Mapped["ConstructionType | None"] = relationship()
    load_index: Mapped["LoadIndex | None"] = relationship()
    speed_index: Mapped["SpeedIndex | None"] = relationship()
    seasonality: Mapped["Seasonality | None"] = relationship()
    tread_pattern: Mapped["TreadPattern | None"] = relationship()
    pattern: Mapped["Pattern | None"] = relationship(back_populates="tires")