from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.security import create_access_token, get_password_hash, verify_password
from ..models.user import User
from ..schemas.user import Token, UserCreate, UserResponse

router = APIRouter(prefix="/auth", tags=["Авторизация"])


async def _authenticate_user(email: str, password: str, db: AsyncSession) -> User:
    """Проверяет учетные данные пользователя по email и паролю."""
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный email или пароль",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register_user(user_data: UserCreate, db: AsyncSession = Depends(get_db)) -> User:
    """Регистрация нового пользователя системы."""
    result = await db.execute(select(User).where(User.email == user_data.email))
    if result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пользователь с таким email уже существует",
        )

    hashed_pwd = get_password_hash(user_data.password)
    new_user = User(email=user_data.email, hashed_password=hashed_pwd)

    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    return new_user


@router.post("/login", response_model=Token)
async def login(user_data: UserCreate, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Авторизация по JSON-телу {email, password}."""
    user = await _authenticate_user(user_data.email, user_data.password, db)
    access_token = create_access_token(data={"sub": str(user.id), "role": user.role.value})
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/token", response_model=Token)
async def login_for_swagger_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """OAuth2-совместимый эндпоинт получения токена по форме (для кнопки Authorize в Swagger UI)."""
    user = await _authenticate_user(form_data.username, form_data.password, db)
    access_token = create_access_token(data={"sub": str(user.id), "role": user.role.value})
    return {"access_token": access_token, "token_type": "bearer"}
