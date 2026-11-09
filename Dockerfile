FROM python:3.14-slim

# Устанавливаем пакетный менеджер uv из официального образа
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Копируем манифесты зависимостей
COPY pyproject.toml uv.lock ./

# Устанавливаем только production-зависимости (без dev-пакетов)
RUN uv sync --frozen --no-dev --no-install-project

# Копируем исходный код, скрипты, документацию и миграции
COPY README.md* alembic.ini ./
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/

EXPOSE 8000

# Запуск API-сервера (миграции выполняются отдельным сервисом migrate в docker-compose)
CMD ["uv", "run", "--no-dev", "uvicorn", "src.server.main:app", "--host", "0.0.0.0", "--port", "8000"]
