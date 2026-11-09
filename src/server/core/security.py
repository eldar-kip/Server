from datetime import datetime, timedelta, timezone
from typing import Any
import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from src.server.core.config import settings

# Реэкспорт настроек для обратной совместимости импортов
SECRET_KEY: str = settings.SECRET_KEY
ALGORITHM: str = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES: int = settings.ACCESS_TOKEN_EXPIRE_MINUTES

# Хэширование паролей (Argon2)
password_hash = PasswordHash((Argon2Hasher(),))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверяет соответствие открытого пароля сохраненному Argon2-хэшу."""
    return password_hash.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Возвращает Argon2-хэш пароля."""
    return password_hash.hash(password)


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Генерирует подписанный JWT-токен с заданным временем жизни."""
    to_encode: dict[str, Any] = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Декодирует и валидирует JWT-токен."""
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
