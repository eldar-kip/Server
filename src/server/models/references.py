from typing import Optional
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base

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
    __tablename__ = "tread_patterns"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))

class Technology(Base):
    __tablename__ = "technologies"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20))
    description: Mapped[Optional[str]] = mapped_column(Text)