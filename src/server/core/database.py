import os
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql+asyncpg://admin:password@localhost:5432/taires_db"
)

engine = create_async_engine(
    SQLALCHEMY_DATABASE_URL, 
    echo=False
)
AsyncSessionLocal = async_sessionmaker(
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False, 
    autoflush=False,
    bind=engine)

async def get_db()->AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal () as session:
        yield session