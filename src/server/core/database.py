from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.server.core.config import settings

SQLALCHEMY_DATABASE_URL: str = settings.DATABASE_URL

engine = create_async_engine(
    SQLALCHEMY_DATABASE_URL,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
    bind=engine,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Зависимость FastAPI для предоставления асинхронной сессии БД."""
    async with AsyncSessionLocal() as session:
        yield session
