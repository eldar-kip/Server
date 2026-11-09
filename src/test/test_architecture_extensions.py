import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from httpx import AsyncClient, Request, Response
import numpy as np
import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import torch
import torch.nn as nn

from src.ml_worker.trainer import TAiresTrainerService
from src.server.core.config import settings
from src.server.models import (
    AuditActionEnum,
    FeedbackAuditLog,
    MatchStatusEnum,
    ProcurementSession,
    RawOfferItem,
    TaskStatusEnum,
    Tire,
    User,
)
from src.server.services.s3_storage import S3ModelStorageService
from src.server.tasks.matching_tasks import _process_session_in_db
from src.server.tasks.training_tasks import _run_training_job_in_db


class _InMemoryS3Mock:
    """Легковесный in-memory мок boto3 S3-клиента для проверки Хранилища S3 с нейросетями."""

    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}

    def head_bucket(self, Bucket: str) -> dict[str, Any]:
        if Bucket not in self.buckets:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket")
        return {"Bucket": Bucket}

    def create_bucket(self, Bucket: str, **kwargs: Any) -> dict[str, Any]:
        self.buckets.add(Bucket)
        return {"Location": f"/{Bucket}"}

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:
        self.objects[(Bucket, Key)] = Path(Filename).read_bytes()

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
        Path(Filename).write_bytes(self.objects[(Bucket, Key)])

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def get_paginator(self, operation_name: str) -> Any:
        outer = self

        class _Paginator:
            def paginate(self, Bucket: str, Prefix: str = "") -> list[dict[str, Any]]:
                if Bucket not in outer.buckets:
                    raise ClientError(
                        {"Error": {"Code": "NoSuchBucket", "Message": "Not Found"}},
                        "ListObjectsV2",
                    )
                contents = [
                    {"Key": k}
                    for (b, k) in sorted(outer.objects.keys())
                    if b == Bucket and k.startswith(Prefix)
                ]
                return [{"Contents": contents}]

        return _Paginator()


class _TinyTrainableBiEncoder(nn.Module):
    """Миниатюрная обучаемая модель PyTorch с интерфейсом SentenceTransformer для быстрого теста tAIres-Trainer."""

    def __init__(self, dim: int = 16) -> None:
        super().__init__()
        self.proj = nn.Linear(dim, dim, bias=False)
        with torch.no_grad():
            self.proj.weight.copy_(torch.eye(dim))
        self._dim = dim

    @property
    def device(self) -> torch.device:
        return torch.device("cpu")

    def _text_to_vec(self, text: str) -> list[float]:
        clean = text.replace("query: ", "").replace("passage: ", "").lower()
        vec = [0.0] * self._dim
        for idx, ch in enumerate(clean):
            vec[(ord(ch) + idx) % self._dim] += 1.0
        return vec

    def tokenize(self, texts: list[str]) -> dict[str, torch.Tensor]:
        mat = torch.tensor([self._text_to_vec(t) for t in texts], dtype=torch.float32)
        return {"features": mat}

    def forward(self, features: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
        out = self.proj(features["features"])
        return {"sentence_embedding": out}

    def encode(
        self,
        texts: list[str],
        batch_size: int = 32,
        normalize_embeddings: bool = True,
        show_progress_bar: bool = False,
    ) -> np.ndarray:
        self.eval()
        with torch.no_grad():
            feat = self.tokenize(texts)
            emb = self.forward(feat)["sentence_embedding"]
            if normalize_embeddings:
                emb = torch.nn.functional.normalize(emb, p=2, dim=1)
            return emb.cpu().numpy()

    def save(self, path: str) -> None:
        out = Path(path)
        out.mkdir(parents=True, exist_ok=True)
        torch.save(self.state_dict(), out / "pytorch_model.bin")
        (out / "config.json").write_text(json.dumps({"dim": self._dim}), encoding="utf-8")


def test_s3_model_storage_roundtrip(tmp_path: Path) -> None:
    """Проверяет загрузку обученной нейросети в S3, чтение реестра S3 и обратное скачивание весов."""
    fake_s3 = _InMemoryS3Mock()
    src_dir = tmp_path / "trained_src"
    src_dir.mkdir()
    (src_dir / "pytorch_model.bin").write_bytes(b"weights-binary-payload")
    (src_dir / "metadata.json").write_text(
        json.dumps({"model_name": "taires-e5", "model_version": "2.1.0", "autotest": {"accuracy_at_1": 0.94}}),
        encoding="utf-8",
    )

    s3_uri = S3ModelStorageService.upload_model_directory(
        local_dir=src_dir,
        model_name="taires-e5",
        model_version="2.1.0",
        bucket="taires-models",
        s3_client=fake_s3,
    )
    assert s3_uri == "s3://taires-models/models/taires-e5/2.1.0"

    listed = S3ModelStorageService.list_models_in_s3(bucket="taires-models", s3_client=fake_s3)
    assert len(listed) == 1
    assert listed[0]["model_version"] == "2.1.0"
    assert listed[0]["s3_uri"] == s3_uri

    cache_dir = tmp_path / "worker_cache"
    downloaded = S3ModelStorageService.download_model_uri(
        s3_uri=s3_uri,
        cache_root=cache_dir,
        s3_client=fake_s3,
    )
    assert (downloaded / "pytorch_model.bin").read_bytes() == b"weights-binary-payload"
    assert (downloaded / ".complete").exists()


async def test_two_tables_plus_token_and_files_endpoints(
    client: AsyncClient,
) -> None:
    """Проверяет контракт 'Две таблицы + токен' (JSON и загрузка двух файлов .csv)."""
    payload = {
        "enterprise_table": [
            {"id": 10, "sku": "Шина_1 Michelin 205/55 R16"},
            {"id": 20, "sku": "Шина_2 Pirelli 225/45 R17"},
            {"id": 30, "sku": "Шина_3 Nokian 195/65 R15"},
        ],
        "supplier_table": [
            {"raw_name": "ШиНа1 Michelin 205/55R16", "price": 19153.0, "quantity": 8},
            {"raw_name": "шина_3 Nokian 195/65 R15", "price": 14800.0, "quantity": 12},
        ],
        "top_k": 3,
    }

    # Без токена доступ запрещен (401)
    unauth = await client.post("/api/v1/ai/match-tables", json=payload)
    assert unauth.status_code == 401

    # Детерминированные векторы для трех эталонов и двух строк поставщика
    def _mock_passages(texts: list[str], batch_size: int = 64) -> list[list[float]]:
        return [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ][: len(texts)]

    def _mock_queries(texts: list[str], batch_size: int = 64) -> list[list[float]]:
        return [
            [0.96, 0.10, 0.0],  # Ближе всего к Шина_1
            [0.02, 0.05, 0.99],  # Ближе всего к Шина_3
        ][: len(texts)]

    with (
        patch("src.ml_worker.vector_search.embed_passages", side_effect=_mock_passages),
        patch("src.ml_worker.vector_search.embed_queries", side_effect=_mock_queries),
    ):
        resp = await client.post(
            "/api/v1/ai/match-tables",
            json=payload,
            headers={"X-AI-Service-Token": settings.AI_SERVICE_TOKEN},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_rows"] == 2
        assert body["items"][0]["enterprise_id"] == 10
        assert body["items"][0]["enterprise_name"] == "Шина_1 Michelin 205/55 R16"
        assert body["items"][0]["supplier_price"] == 19153.0
        assert body["items"][0]["confidence_score"] > 0.9

        assert body["items"][1]["enterprise_id"] == 30
        assert body["items"][1]["enterprise_name"] == "Шина_3 Nokian 195/65 R15"

        # Проверяем также файловый вариант POST /api/v1/ai/match-files
        ent_csv = "id;Предприятие\n10;Шина_1 Michelin 205/55 R16\n20;Шина_2 Pirelli 225/45 R17\n30;Шина_3 Nokian 195/65 R15\n"
        sup_csv = "Наименование;Цена;Остаток\nШиНа1 Michelin 205/55R16;19153;8\nшина_3 Nokian 195/65 R15;14800;12\n"

        files_resp = await client.post(
            "/api/v1/ai/match-files",
            files={
                "enterprise_file": ("enterprise.csv", ent_csv.encode("utf-8"), "text/csv"),
                "supplier_file": ("supplier.csv", sup_csv.encode("utf-8"), "text/csv"),
            },
            headers={"X-AI-Service-Token": settings.AI_SERVICE_TOKEN},
        )
        assert files_resp.status_code == 200
        assert files_resp.json()["total_rows"] == 2


async def test_taires_trainer_full_lifecycle_and_model_activation(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    buyer_user: User,
    buyer_token: str,
    admin_token: str,
) -> None:
    """
    Проверяет полный контур tAIres-Trainer:
    сбор датасета из FeedbackAuditLog, запуск обучения со всеми 8 параметрами,
    расчет телеметрии и автотеста, выгрузку в S3 и переключение активной модели.
    """
    tire1 = Tire(sku="Michelin Pilot Sport 4 225/45 R17", width_mm=225, profile_ratio=45, diameter_inch=17)
    tire2 = Tire(sku="Nokian Hakkapeliitta 9 205/55 R16", width_mm=205, profile_ratio=55, diameter_inch=16)
    db_session.add_all([tire1, tire2])
    await db_session.flush()

    proc_sess = ProcurementSession(
        user_id=buyer_user.id,
        title="Сессия для обучения",
        status=TaskStatusEnum.COMPLETED,
        total_items=1,
        matched_items=1,
    )
    db_session.add(proc_sess)
    await db_session.flush()

    raw_item = RawOfferItem(
        session_id=proc_sess.id,
        raw_name="Автошина Мишлен ПС4 225/45/17",
        price=12500.0,
        quantity=4,
        matched_tire_id=tire1.id,
        confidence_score=1.0,
        match_status=MatchStatusEnum.MANUAL,
    )
    db_session.add(raw_item)
    await db_session.flush()

    db_session.add(
        FeedbackAuditLog(
            session_id=proc_sess.id,
            item_id=raw_item.id,
            user_id=buyer_user.id,
            action=AuditActionEnum.MANUAL_REMATCH,
            new_tire_id=tire1.id,
        )
    )
    await db_session.commit()

    # 1. Выгрузка собранного датасета
    ds_resp = await client.get(
        "/api/v1/ml/dataset?format=json",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert ds_resp.status_code == 200
    ds_items = ds_resp.json()
    assert any("Мишлен" in row["query"] for row in ds_items)

    # 2. Закупщик (Buyer) не имеет права запускать обучение (403)
    train_payload = {
        "model_name": "taires-custom-e5",
        "model_version": "1.2.0",
        "base_model": "intfloat/multilingual-e5-large",
        "batch_size": 2,
        "epochs": 2,
        "learning_rate": 0.001,
        "val_split": 0.25,
        "dataset": [
            {"query": "Шина_1 Michelin 225/45 R17", "positive_passage": "Michelin Pilot Sport 4 225/45 R17"},
            {"query": "Шина_2 Nokian 205/55 R16", "positive_passage": "Nokian Hakkapeliitta 9 205/55 R16"},
            {"query": "Шина_3 Pirelli 215/60 R16", "positive_passage": "Pirelli Cinturato P7 215/60 R16"},
            {"query": "Шина_4 Continental 195/65 R15", "positive_passage": "Continental IceContact 3 195/65 R15"},
        ],
        "use_feedback_log": True,
    }
    forbidden = await client.post(
        "/api/v1/ml/train",
        json=train_payload,
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert forbidden.status_code == 403

    # 3. Администратор создает задачу обучения в tAIres-Trainer
    fake_s3 = _InMemoryS3Mock()
    with patch("src.server.api.v1.ml_ops.run_training_job.delay") as mock_delay:
        create_resp = await client.post(
            "/api/v1/ml/train",
            json=train_payload,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert create_resp.status_code == 201
        job_data = create_resp.json()
        job_id = job_data["id"]
        mock_delay.assert_called_once()

    # 4. Выполняем реальный цикл обучения tAIres-Trainer на мини-модели PyTorch + выгрузку в S3
    with (
        patch("sentence_transformers.SentenceTransformer", return_value=_TinyTrainableBiEncoder(dim=16)),
        patch.object(S3ModelStorageService, "is_configured", return_value=True),
        patch.object(S3ModelStorageService, "get_client", return_value=fake_s3),
        patch("src.server.tasks.training_tasks.publish_training_telemetry"),
    ):
        explicit_pairs = [[p["query"], p["positive_passage"]] for p in train_payload["dataset"]]
        res = await _run_training_job_in_db(
            job_id=job_id,
            explicit_pairs=explicit_pairs,
            use_feedback_log=True,
            session_factory=db_session_factory,
        )
        assert res["status"] == "COMPLETED"
        assert res["s3_uri"] == "s3://taires-models/models/taires-custom-e5/1.2.0"

    # 5. Проверяем карточку задачи (телеметрия по 2 эпохам, точность автотеста, метадата)
    job_detail = await client.get(
        f"/api/v1/ml/jobs/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert job_detail.status_code == 200
    detail = job_detail.json()
    assert detail["status"] == "COMPLETED"
    assert len(detail["telemetry"]) == 2
    assert detail["autotest_accuracy"] is not None
    assert detail["model_metadata"]["model_version"] == "1.2.0"

    # 6. Активируем обученную модель из S3 на Сервере ИИ
    with patch("src.server.api.v1.ml_ops.set_active_model_uri") as mock_set_uri:
        act_resp = await client.post(
            f"/api/v1/ml/models/{job_id}/activate",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert act_resp.status_code == 200
        assert act_resp.json()["active_model_uri"] == "s3://taires-models/models/taires-custom-e5/1.2.0"
        mock_set_uri.assert_called_once_with("s3://taires-models/models/taires-custom-e5/1.2.0")


async def test_crm_matched_nomenclature_table_and_external_ai_mode(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    buyer_user: User,
    buyer_token: str,
) -> None:
    """
    Проверяет:
    1) межсервисный режим EXTERNAL_AI_SERVER_URL в matching_tasks ('Сервер' -> 'Сервер, запускающий ИИ');
    2) эндпоинт возврата сопоставленной номенклатуры для Сайта / Модуля CRM (JSON, CSV, XLSX).
    """
    tire_a = Tire(sku="Шина_1", width_mm=205, profile_ratio=55, diameter_inch=16, search_text="Шина_1 205/55 R16")
    tire_b = Tire(sku="Шина_2", width_mm=225, profile_ratio=45, diameter_inch=17, search_text="Шина_2 225/45 R17")
    db_session.add_all([tire_a, tire_b])
    await db_session.flush()

    proc = ProcurementSession(
        user_id=buyer_user.id,
        title="CRM таблица",
        status=TaskStatusEnum.PENDING,
        total_items=2,
        matched_items=0,
    )
    db_session.add(proc)
    await db_session.flush()

    db_session.add_all(
        [
            RawOfferItem(session_id=proc.id, raw_name="ШиНа1", price=19153.0, quantity=4),
            RawOfferItem(session_id=proc.id, raw_name="ШинА_2", price=20030.4, quantity=4),
        ]
    )
    await db_session.commit()

    fake_ai_response = Response(
        200,
        json={
            "active_model": "s3://taires-models/models/taires-custom-e5/1.2.0",
            "total_rows": 2,
            "items": [
                {
                    "row_number": 1,
                    "enterprise_id": tire_a.id,
                    "enterprise_name": "Шина_1",
                    "supplier_name": "ШиНа1",
                    "supplier_price": 19153.0,
                    "confidence_score": 0.91,
                    "match_status": "AUTO",
                    "candidates": [{"tire_id": tire_a.id, "sku": "Шина_1", "score": 0.91}],
                },
                {
                    "row_number": 2,
                    "enterprise_id": tire_b.id,
                    "enterprise_name": "Шина_2",
                    "supplier_name": "ШинА_2",
                    "supplier_price": 20030.4,
                    "confidence_score": 0.65,
                    "match_status": "NEEDS_REVIEW",
                    "candidates": [{"tire_id": tire_b.id, "sku": "Шина_2", "score": 0.65}],
                },
            ],
        },
        request=Request("POST", "http://ai-runner:8000/api/v1/ai/match-tables"),
    )

    mock_post = MagicMock()

    async def _async_post(*args: Any, **kwargs: Any) -> Response:
        mock_post(*args, **kwargs)
        return fake_ai_response

    with (
        patch.object(settings, "EXTERNAL_AI_SERVER_URL", "http://ai-runner:8000"),
        patch("src.server.tasks.matching_tasks.publish_progress"),
        patch("httpx.AsyncClient.post", side_effect=_async_post),
    ):
        await _process_session_in_db(proc.id, session_factory=db_session_factory)
        mock_post.assert_called_once()
        sent_headers = mock_post.call_args.kwargs["headers"]
        assert sent_headers["X-AI-Service-Token"] == settings.AI_SERVICE_TOKEN

    # Проверяем возврат таблицы сопоставленной номенклатуры в JSON, CSV и XLSX
    json_resp = await client.get(
        f"/api/v1/procurement/{proc.id}/matched-table?format=json",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert json_resp.status_code == 200
    rows = json_resp.json()
    assert len(rows) == 2
    assert rows[0]["enterprise_name"] == "Шина_1"
    assert rows[0]["supplier_name"] == "ШиНа1"
    assert rows[0]["supplier_price"] == 19153.0
    assert rows[0]["confidence_score"] == pytest.approx(0.91)

    csv_resp = await client.get(
        f"/api/v1/procurement/{proc.id}/matched-table?format=csv",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert csv_resp.status_code == 200
    csv_text = csv_resp.content.decode("utf-8-sig")
    assert "Предприятие;Поставщик;Цена поставщика;Точность совпадения" in csv_text
    assert "Шина_1;ШиНа1;19153.0;0.91" in csv_text

    xlsx_resp = await client.get(
        f"/api/v1/procurement/{proc.id}/matched-table?format=xlsx",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert xlsx_resp.status_code == 200
    df = pd.read_excel(io.BytesIO(xlsx_resp.content), engine="openpyxl")
    assert list(df["Предприятие"]) == ["Шина_1", "Шина_2"]


def test_trainer_split_dataset_bounds() -> None:
    """Проверяет корректное разбиение датасета по проценту валидации val_split."""
    pairs = [(f"q_{i}", f"p_{i}") for i in range(10)]
    train_p, val_p = TAiresTrainerService.split_dataset(pairs, val_split=0.2)
    assert len(val_p) == 2
    assert len(train_p) == 8
