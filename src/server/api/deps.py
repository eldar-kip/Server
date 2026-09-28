from collections.abc import Callable
from typing import Any
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.security import ALGORITHM, SECRET_KEY
from ..models.user import User

# Указываем относительный путь к роуту логина для Swagger UI
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

async def get_current_user(
    token: str = Depends(oauth2_scheme), 
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Извлекает токен из заголовка Authorization, валидирует его и возвращает объект User из БД.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось проверить учетные данные (токен недействителен или истек)",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        # Декодируем токен
        payload: dict[str, Any] = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])  # type: ignore
        user_id_raw = payload.get("sub")
        if user_id_raw is None:
            raise credentials_exception
        user_id = int(user_id_raw)
    except (jwt.PyJWTError, ValueError):
        # Ошибка декодирования токена или некорректный ID
        raise credentials_exception
        
    # Ищем пользователя в асинхронной базе данных
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    
    if user is None:
        raise credentials_exception
        
    return user

def require_role(required_role: str) -> Callable[[User], User]:
    """
    Генератор зависимостей для проверки конкретной роли.
    Пример: Depends(require_role("Admin"))
    """
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role.value.lower() != required_role.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Доступ запрещен. Требуется роль: {required_role}"
            )
        return current_user
        
    return role_checker