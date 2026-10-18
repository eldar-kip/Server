# Используем легковесный образ Python 3.14 (согласно твоему pyproject.toml)
FROM python:3.14-slim

# Устанавливаем пакетный менеджер uv напрямую из официального образа
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем манифесты зависимостей и описание
COPY pyproject.toml uv.lock README.md* ./

# Устанавливаем зависимости
RUN uv sync --no-install-project

# Копируем исходный код сервера и миграции
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Автоматически применяем миграции и запускаем FastAPI
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn src.server.main:app --host 0.0.0.0 --port 8000"]