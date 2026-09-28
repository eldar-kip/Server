from typing import TYPE_CHECKING, Optional
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

if TYPE_CHECKING:
    from .tire import Tire

class SpeedIndex(Base):
    __tablename__ = "speed_indices"
    
    code: Mapped[str] = mapped_column(String(2), primary_key=True)
    max_speed_kmh: Mapped[int]

class LoadIndex(Base):
    __tablename__ = "load_indices"
    
    code: Mapped[int] = mapped_column(primary_key=True)
    max_load_kg: Mapped[int]

class ConstructionType(Base):
    __tablename__ = "construction_types"
    
    code: Mapped[str] = mapped_column(String(1), primary_key=True)
    name: Mapped[str] = mapped_column(String(50))

class Seasonality(Base):
    __tablename__ = "seasonalities"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))

class TreadPattern(Base):
    """Тип рисунка протектора (симметричный, асимметричный, направленный)"""
    __tablename__ = "tread_patterns"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))

class Technology(Base):
    __tablename__ = "technologies"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20))
    description: Mapped[Optional[str]] = mapped_column(Text)

class Manufacturer(Base):
    """Производитель (Michelin, Pirelli, Nokian и др.)"""
    __tablename__ = "manufacturers"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    country: Mapped[Optional[str]] = mapped_column(String(50))

    # Связь: у одного производителя много моделей
    patterns: Mapped[list["Pattern"]] = relationship(back_populates="manufacturer")

class Pattern(Base):
    """Модель / линейка шины (например, Pilot Sport 4)"""
    __tablename__ = "patterns"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    manufacturer_id: Mapped[int] = mapped_column(ForeignKey("manufacturers.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    seasonality_id: Mapped[Optional[int]] = mapped_column(ForeignKey("seasonalities.id")) 

    # Связи
    manufacturer: Mapped["Manufacturer"] = relationship(back_populates="patterns")
    seasonality: Mapped["Seasonality | None"] = relationship()
    tires: Mapped[list["Tire"]] = relationship(back_populates="pattern")