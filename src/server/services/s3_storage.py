from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.server.core.config import settings

logger = logging.getLogger(__name__)


class S3ModelStorageService:
    """
    Сервис работы с объектным хранилищем S3 / MinIO ('Хранилище S3 с нейросетями').
    Отвечает за выгрузку обученных весов, телеметрии и метаданных из tAIres-Trainer
    и их скачивание на Сервер, запускающий ИИ.
    """

    @staticmethod
    def is_configured() -> bool:
        return bool(settings.S3_ENDPOINT_URL.strip())

    @classmethod
    def get_client(cls) -> Any:
        endpoint = settings.S3_ENDPOINT_URL.strip() or None
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
        )

    @classmethod
    def ensure_bucket_exists(cls, bucket: str | None = None, s3_client: Any = None) -> str:
        """Проверяет наличие S3-бакета моделей и создает его при отсутствии."""
        target_bucket = bucket or settings.S3_BUCKET_MODELS
        client = s3_client or cls.get_client()
        try:
            client.head_bucket(Bucket=target_bucket)
        except ClientError:
            create_kwargs: dict[str, Any] = {"Bucket": target_bucket}
            if settings.S3_REGION and settings.S3_REGION != "us-east-1":
                create_kwargs["CreateBucketConfiguration"] = {
                    "LocationConstraint": settings.S3_REGION
                }
            client.create_bucket(**create_kwargs)
            logger.info("Создан S3-бакет для нейросетей: %s", target_bucket)
        return target_bucket

    @classmethod
    def upload_model_directory(
        cls,
        local_dir: Path,
        model_name: str,
        model_version: str,
        bucket: str | None = None,
        s3_client: Any = None,
    ) -> str:
        """Рекурсивно загружает директорию обученной модели и metadata.json в S3."""
        client = s3_client or cls.get_client()
        target_bucket = cls.ensure_bucket_exists(bucket=bucket, s3_client=client)
        safe_name = model_name.strip().replace(" ", "_")
        safe_ver = model_version.strip().replace(" ", "_")
        prefix = f"models/{safe_name}/{safe_ver}"

        for file_path in sorted(local_dir.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(local_dir).as_posix()
                key = f"{prefix}/{rel}"
                client.upload_file(str(file_path), target_bucket, key)

        s3_uri = f"s3://{target_bucket}/{prefix}"
        logger.info("Модель %s:%s выгружена в %s", model_name, model_version, s3_uri)
        return s3_uri

    @staticmethod
    def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
        """Разбирает 's3://bucket/prefix/path' на (bucket, prefix)."""
        if not s3_uri.startswith("s3://"):
            raise ValueError(f"Некорректный S3 URI: {s3_uri}")
        without_scheme = s3_uri[len("s3://") :].strip("/")
        bucket, _, prefix = without_scheme.partition("/")
        if not bucket or not prefix:
            raise ValueError(f"S3 URI должен содержать бакет и ключ: {s3_uri}")
        return bucket, prefix

    @classmethod
    def download_model_uri(
        cls,
        s3_uri: str,
        cache_root: Path | None = None,
        s3_client: Any = None,
    ) -> Path:
        """Скачивает веса нейросети из S3 в локальный кэш для запуска инференса."""
        bucket, prefix = cls.parse_s3_uri(s3_uri)
        base_cache = cache_root or Path(settings.HF_HOME or "./models_cache")
        target_dir = base_cache / "s3_cache" / bucket / prefix
        marker = target_dir / ".complete"

        if marker.exists():
            return target_dir

        client = s3_client or cls.get_client()
        paginator = client.get_paginator("list_objects_v2")
        found_any = False

        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = str(obj["Key"])
                if key.endswith("/"):
                    continue
                rel = key[len(prefix) :].lstrip("/")
                dest = target_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(bucket, key, str(dest))
                found_any = True

        if not found_any:
            raise FileNotFoundError(f"В S3 по адресу {s3_uri} не найдены файлы модели.")

        marker.write_text("ok", encoding="utf-8")
        logger.info("Модель скачана из %s в %s", s3_uri, target_dir)
        return target_dir

    @classmethod
    def list_models_in_s3(
        cls,
        bucket: str | None = None,
        s3_client: Any = None,
    ) -> list[dict[str, Any]]:
        """Возвращает список метаданных всех моделей, хранящихся в S3-бакете."""
        if not cls.is_configured() and s3_client is None:
            return []
        client = s3_client or cls.get_client()
        target_bucket = bucket or settings.S3_BUCKET_MODELS
        try:
            paginator = client.get_paginator("list_objects_v2")
            items: list[dict[str, Any]] = []
            for page in paginator.paginate(Bucket=target_bucket, Prefix="models/"):
                for obj in page.get("Contents", []):
                    key = str(obj["Key"])
                    if key.endswith("/metadata.json"):
                        body = client.get_object(Bucket=target_bucket, Key=key)["Body"].read()
                        meta = json.loads(body.decode("utf-8"))
                        prefix = key[: -len("/metadata.json")]
                        meta["s3_uri"] = f"s3://{target_bucket}/{prefix}"
                        items.append(meta)
            return items
        except ClientError:
            return []
