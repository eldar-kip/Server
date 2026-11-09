from collections.abc import Callable
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.security import decode_access_token
from ..models.procurement import ProcurementSession
from ..models.user import RoleEnum, User

# Путь к OAuth2 form-эндпоинту для корректной работы кнопки Authorize в Swagger UI
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Извлекает токен из заголовка Authorization, валидирует его и возвращает объект User."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось проверить учетные данные (токен недействителен или истек)",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_access_token(token)
        user_id_raw = payload.get("sub")
        if user_id_raw is None:
            raise credentials_exception
        user_id = int(user_id_raw)
    except (jwt.PyJWTError, ValueError):
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception

    return user


async def authenticate_ws_token(token: str | None, db: AsyncSession) -> User | None:
    """Валидирует JWT-токен из query-параметра WebSocket-подключения (?token=...)."""
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        user_id_raw = payload.get("sub")
        if user_id_raw is None:
            return None
        user_id = int(user_id_raw)
    except (jwt.PyJWTError, ValueError):
        return None

    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalars().first()


def ensure_session_access(session: ProcurementSession, current_user: User) -> None:
    """Проверяет права доступа пользователя к закупочной сессии (Admin видит все, Buyer — свои)."""
    if current_user.role == RoleEnum.ADMIN:
        return
    if session.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ к данной закупочной сессии запрещен",
        )


def require_role(required_role: str) -> Callable[[User], User]:
    """Генератор зависимостей для проверки конкретной роли пользователя."""

    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role.value.lower() != required_role.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Доступ запрещен. Требуется роль: {required_role}",
            )
        return current_user

    return role_checker
