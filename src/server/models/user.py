import enum
from sqlalchemy import String, Enum
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base

# Определяем доступные роли в системе
class RoleEnum(str, enum.Enum):
    ADMIN = "Admin"
    BUYER = "Buyer"

# Модель пользователя
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    
    # Роль пользователя (по умолчанию задаем Buyer)
    role: Mapped[RoleEnum] = mapped_column(Enum(RoleEnum), default=RoleEnum.BUYER, nullable=False)