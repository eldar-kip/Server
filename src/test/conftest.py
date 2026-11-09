from collections.abc import AsyncGenerator
import pytest
from httpx import ASGITransport, AsyncClient
from pgvector.sqlalchemy import Vector
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from src.server.core.database import get_db
from src.server.core.security import create_access_token, get_password_hash
from src.server.main import app
from src.server.models import Base, RoleEnum, User


@compiles(Vector, "sqlite")
def compile_vector_sqlite(type_: Vector, compiler: object, **kw: object) -> str:
    """Позволяет создавать колонки pgvector Vector в локальной тестовой БД SQLite."""
    return "TEXT"


@pytest.fixture
async def db_session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Создает изолированную in-memory асинхронную БД на каждый тест."""
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(test_engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    try:
        yield factory
    finally:
        await test_engine.dispose()


@pytest.fixture
async def db_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Асинхронная сессия БД для unit- и интеграционных тестов."""
    async with db_session_factory() as session:
        yield session


@pytest.fixture
async def client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """Тестовый HTTP-клиент FastAPI с переопределенной зависимостью get_db."""

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with db_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def buyer_user(db_session: AsyncSession) -> User:
    user = User(
        email="buyer@taires.com",
        hashed_password=get_password_hash("buyerpass"),
        role=RoleEnum.BUYER,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def other_buyer_user(db_session: AsyncSession) -> User:
    user = User(
        email="other@taires.com",
        hashed_password=get_password_hash("otherpass"),
        role=RoleEnum.BUYER,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = User(
        email="admin@taires.com",
        hashed_password=get_password_hash("adminpass"),
        role=RoleEnum.ADMIN,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def buyer_token(buyer_user: User) -> str:
    return create_access_token({"sub": str(buyer_user.id), "role": buyer_user.role.value})


@pytest.fixture
def other_buyer_token(other_buyer_user: User) -> str:
    return create_access_token({"sub": str(other_buyer_user.id), "role": other_buyer_user.role.value})


@pytest.fixture
def admin_token(admin_user: User) -> str:
    return create_access_token({"sub": str(admin_user.id), "role": admin_user.role.value})
