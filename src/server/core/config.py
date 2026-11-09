from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Централизованная конфигурация приложения (читается из переменных окружения и .env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Подключение к PostgreSQL (asyncpg) и Redis
    DATABASE_URL: str = "postgresql+asyncpg://admin:password@localhost:5432/taires_db"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Безопасность и JWT
    SECRET_KEY: str = "dev-secret-key-change-in-production-32chars"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 дней

    # Межсервисный протокол "Две таблицы + токен" (Сервер <-> Сервер, запускающий ИИ)
    AI_SERVICE_TOKEN: str = "dev-ai-service-secret-token"
    EXTERNAL_AI_SERVER_URL: str = ""

    # Хранилище S3 / MinIO для обученных нейросетей и метаданных
    S3_ENDPOINT_URL: str = ""
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET_MODELS: str = "taires-models"
    S3_REGION: str = "us-east-1"

    # ML-модель векторизации (Bi-Encoder) и пороги сопоставления
    EMBEDDING_MODEL: str = "intfloat/multilingual-e5-large"
    EMBEDDING_DIM: int = 1024
    HF_HOME: str | None = None
    MATCH_BATCH_SIZE: int = 50
    MATCH_TOP_K: int = 15
    MATCH_AUTO_THRESHOLD: float = 0.85
    MATCH_REVIEW_THRESHOLD: float = 0.70
    HNSW_EF_SEARCH: int = 40

    # Экспорт и интеграция с внешней ERP / 1С по Webhook
    ERP_WEBHOOK_URL: str = ""
    ERP_WEBHOOK_SECRET: str = "dev-erp-webhook-hmac-secret"
    ERP_WEBHOOK_TIMEOUT_SECONDS: float = 15.0


@lru_cache
def get_settings() -> Settings:
    """Возвращает кэшированный экземпляр настроек."""
    return Settings()


settings = get_settings()
