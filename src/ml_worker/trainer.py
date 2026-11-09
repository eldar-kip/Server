from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import random
import tempfile
import time
from typing import Any

import numpy as np

from src.ml_worker.vector_search import PASSAGE_PREFIX, QUERY_PREFIX, resolve_model_source
from src.server.core.config import settings
from src.server.services.s3_storage import S3ModelStorageService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TrainingConfig:
    """Все входные параметры блока 'Ввод' для tAIres-Trainer."""

    model_name: str
    model_version: str
    base_model: str
    batch_size: int
    epochs: int
    learning_rate: float
    val_split: float
    pairs: list[tuple[str, str]]  # (raw_supplier_query, canonical_enterprise_passage)


@dataclass(slots=True)
class TrainingResult:
    """Все выходные артефакты блока 'Вывод' из tAIres-Trainer."""

    s3_uri: str
    autotest_accuracy: float
    telemetry: list[dict[str, Any]]
    metadata: dict[str, Any]


class TAiresTrainerService:
    """
    Модуль обучения и валидации нейросетей tAIres-Trainer:
    - делит датасет по val_split на train и holdout-автотест;
    - дообучает Bi-Encoder контрастивным InfoNCE loss (MultipleNegativesRanking);
    - собирает телеметрию по каждой эпохе;
    - запускает финальный автотест точности сопоставления (Accuracy@1, Accuracy@5, MRR);
    - сохраняет модель + metadata.json + telemetry.json в Хранилище S3 (или локальный кэш).
    """

    @staticmethod
    def split_dataset(
        pairs: list[tuple[str, str]],
        val_split: float,
        seed: int = 42,
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Разбивает пары (запрос, эталон) на обучающую и валидационную выборки."""
        cleaned = [(q.strip(), p.strip()) for q, p in pairs if q.strip() and p.strip()]
        if not cleaned:
            raise ValueError("Датасет для обучения пуст.")
        if len(cleaned) == 1:
            return cleaned, cleaned

        rng = random.Random(seed)
        shuffled = list(cleaned)
        rng.shuffle(shuffled)

        clamped_split = max(0.05, min(0.5, float(val_split)))
        val_count = max(1, min(len(shuffled) - 1, int(round(len(shuffled) * clamped_split))))
        val_pairs = shuffled[:val_count]
        train_pairs = shuffled[val_count:]
        return train_pairs, val_pairs

    @staticmethod
    def evaluate_autotest(
        model: Any,
        eval_pairs: list[tuple[str, str]],
        catalog_passages: list[str],
        batch_size: int = 32,
    ) -> dict[str, float]:
        """
        Запускает автотест на валидационной выборке: ищет правильный эталон среди всех
        уникальных позиций каталога и считает Accuracy@1, Accuracy@5, MRR и средний косинус.
        """
        unique_catalog = list(dict.fromkeys(catalog_passages))
        passage_to_idx = {p: idx for idx, p in enumerate(unique_catalog)}

        p_vecs = np.asarray(
            model.encode(
                [f"{PASSAGE_PREFIX}{p}" for p in unique_catalog],
                batch_size=batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            ),
            dtype=float,
        )
        q_vecs = np.asarray(
            model.encode(
                [f"{QUERY_PREFIX}{q}" for q, _ in eval_pairs],
                batch_size=batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            ),
            dtype=float,
        )

        sims = q_vecs @ p_vecs.T  # (N_eval, N_catalog)
        hits_at_1 = 0
        hits_at_5 = 0
        reciprocal_ranks: list[float] = []
        pos_cosines: list[float] = []

        for row_idx, (_, target_p) in enumerate(eval_pairs):
            target_idx = passage_to_idx[target_p]
            row_sims = sims[row_idx]
            pos_cosines.append(float(row_sims[target_idx]))

            ranked = np.argsort(-row_sims)
            rank_positions = np.where(ranked == target_idx)[0]
            rank = int(rank_positions[0]) + 1 if len(rank_positions) > 0 else len(unique_catalog)

            if rank == 1:
                hits_at_1 += 1
            if rank <= 5:
                hits_at_5 += 1
            reciprocal_ranks.append(1.0 / rank)

        n = len(eval_pairs)
        return {
            "accuracy_at_1": round(hits_at_1 / n, 4),
            "accuracy_at_5": round(hits_at_5 / n, 4),
            "mrr": round(float(np.mean(reciprocal_ranks)), 4),
            "mean_positive_cosine": round(float(np.mean(pos_cosines)), 4),
        }

    @classmethod
    def train_and_publish(
        cls,
        config: TrainingConfig,
        on_epoch_telemetry: Callable[[dict[str, Any]], None] | None = None,
    ) -> TrainingResult:
        """Выполняет полный цикл обучения tAIres-Trainer, автотест и выгрузку модели в S3."""
        import torch
        import torch.nn.functional as F
        from sentence_transformers import SentenceTransformer

        if settings.HF_HOME:
            import os

            os.environ.setdefault("HF_HOME", settings.HF_HOME)

        train_pairs, val_pairs = cls.split_dataset(config.pairs, config.val_split)
        all_passages = [p for _, p in config.pairs]

        resolved_base = resolve_model_source(config.base_model)
        model = SentenceTransformer(resolved_base)
        device = model.device

        optimizer = torch.optim.AdamW(model.parameters(), lr=float(config.learning_rate))
        temperature_scale = 20.0
        batch_sz = max(1, int(config.batch_size))

        telemetry: list[dict[str, Any]] = []

        for epoch_idx in range(1, int(config.epochs) + 1):
            t0 = time.perf_counter()
            model.train()

            epoch_pairs = list(train_pairs)
            random.Random(epoch_idx).shuffle(epoch_pairs)

            batch_losses: list[float] = []
            for start in range(0, len(epoch_pairs), batch_sz):
                batch = epoch_pairs[start : start + batch_sz]
                q_texts = [f"{QUERY_PREFIX}{q}" for q, _ in batch]
                p_texts = [f"{PASSAGE_PREFIX}{p}" for _, p in batch]

                q_feat = {k: v.to(device) for k, v in model.tokenize(q_texts).items()}
                p_feat = {k: v.to(device) for k, v in model.tokenize(p_texts).items()}

                optimizer.zero_grad()
                q_emb = F.normalize(model(q_feat)["sentence_embedding"], p=2, dim=1)
                p_emb = F.normalize(model(p_feat)["sentence_embedding"], p=2, dim=1)

                logits = (q_emb @ p_emb.T) * temperature_scale
                labels = torch.arange(len(batch), device=device)
                loss = F.cross_entropy(logits, labels)
                loss.backward()
                optimizer.step()

                batch_losses.append(float(loss.detach().cpu().item()))

            model.eval()
            eval_metrics = cls.evaluate_autotest(
                model=model,
                eval_pairs=val_pairs,
                catalog_passages=all_passages,
                batch_size=batch_sz,
            )
            duration_sec = round(time.perf_counter() - t0, 3)

            epoch_entry = {
                "epoch": epoch_idx,
                "total_epochs": int(config.epochs),
                "train_loss": round(float(np.mean(batch_losses)), 6),
                "val_accuracy_at_1": eval_metrics["accuracy_at_1"],
                "val_accuracy_at_5": eval_metrics["accuracy_at_5"],
                "val_mrr": eval_metrics["mrr"],
                "duration_sec": duration_sec,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            telemetry.append(epoch_entry)
            if on_epoch_telemetry is not None:
                on_epoch_telemetry(epoch_entry)

        # Финальный автотест обученной модели
        final_autotest = cls.evaluate_autotest(
            model=model,
            eval_pairs=val_pairs,
            catalog_passages=all_passages,
            batch_size=batch_sz,
        )

        metadata: dict[str, Any] = {
            "model_name": config.model_name,
            "model_version": config.model_version,
            "base_model": config.base_model,
            "hyperparameters": {
                "batch_size": int(config.batch_size),
                "epochs": int(config.epochs),
                "learning_rate": float(config.learning_rate),
                "val_split": float(config.val_split),
            },
            "dataset": {
                "total_pairs": len(config.pairs),
                "train_pairs": len(train_pairs),
                "val_pairs": len(val_pairs),
            },
            "autotest": final_autotest,
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        with tempfile.TemporaryDirectory(prefix="taires_trainer_") as tmp_str:
            out_dir = Path(tmp_str) / "artifact"
            out_dir.mkdir(parents=True, exist_ok=True)
            model.save(str(out_dir))
            (out_dir / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (out_dir / "telemetry.json").write_text(
                json.dumps(telemetry, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            if S3ModelStorageService.is_configured():
                storage_uri = S3ModelStorageService.upload_model_directory(
                    local_dir=out_dir,
                    model_name=config.model_name,
                    model_version=config.model_version,
                )
            else:
                local_store = (
                    Path(settings.HF_HOME or "./models_cache")
                    / "trained"
                    / config.model_name.strip().replace(" ", "_")
                    / config.model_version.strip().replace(" ", "_")
                )
                local_store.mkdir(parents=True, exist_ok=True)
                for item in out_dir.rglob("*"):
                    if item.is_file():
                        dest = local_store / item.relative_to(out_dir)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(item.read_bytes())
                storage_uri = str(local_store.resolve())

        metadata["storage_uri"] = storage_uri
        return TrainingResult(
            s3_uri=storage_uri,
            autotest_accuracy=final_autotest["accuracy_at_1"],
            telemetry=telemetry,
            metadata=metadata,
        )
