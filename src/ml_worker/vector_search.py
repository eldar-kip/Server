from __future__ import annotations

import logging
import math
import os
from typing import TYPE_CHECKING, Any

import numpy as np
import redis
from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.server.core.config import settings
from src.server.models.procurement import MatchStatusEnum
from src.server.models.tire import Tire
from src.server.services.s3_storage import S3ModelStorageService

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_model_instance: SentenceTransformer | None = None
_loaded_model_uri: str | None = None

ACTIVE_MODEL_REDIS_KEY = "ml:active_model_uri"
PASSAGE_PREFIX = "passage: "
QUERY_PREFIX = "query: "


def get_configured_model_uri() -> str:
    """Читает URI активной модели из Redis (если активирована версия из S3) либо берет дефолт из настроек."""
    try:
        r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=0.5)
        active_uri = r.get(ACTIVE_MODEL_REDIS_KEY)
        if active_uri and str(active_uri).strip():
            return str(active_uri).strip()
    except Exception:
        pass
    return settings.EMBEDDING_MODEL


def set_active_model_uri(model_uri: str) -> None:
    """Устанавливает новую активную модель в Redis для горячего переключения воркеров ИИ."""
    r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    r.set(ACTIVE_MODEL_REDIS_KEY, model_uri.strip())


def resolve_model_source(model_uri: str) -> str:
    """Если передан S3 URI ('s3://...'), скачивает веса из S3 в локальный кэш и возвращает путь."""
    if model_uri.startswith("s3://"):
        local_dir = S3ModelStorageService.download_model_uri(model_uri)
        return str(local_dir)
    return model_uri


def get_embedding_model(force_uri: str | None = None) -> SentenceTransformer:
    """
    Ленивая загрузка singleton-модели Bi-Encoder с поддержкой горячей подгрузки новых версий из S3.
    """
    global _model_instance, _loaded_model_uri
    desired_uri = force_uri or get_configured_model_uri()

    if _model_instance is None or _loaded_model_uri != desired_uri:
        if settings.HF_HOME:
            os.environ.setdefault("HF_HOME", settings.HF_HOME)
        from sentence_transformers import SentenceTransformer

        resolved_path = resolve_model_source(desired_uri)
        logger.info("Загрузка модели эмбеддингов: %s (источник: %s)", desired_uri, resolved_path)
        _model_instance = SentenceTransformer(resolved_path)
        _loaded_model_uri = desired_uri

    return _model_instance


def build_tire_search_text(tire: Tire) -> str:
    """
    Формирует нормализованное текстовое описание шины для векторизации:
    производитель + модель + размерность (205/55 R16 91V) + сезонность + технологии + SKU.
    """
    parts: list[str] = []

    pattern = tire.__dict__.get("pattern")
    if pattern is not None:
        manufacturer = pattern.__dict__.get("manufacturer")
        if manufacturer is not None and manufacturer.name:
            parts.append(manufacturer.name.strip())
        if pattern.name:
            parts.append(pattern.name.strip())

    const_code = tire.construction_type_code or "R"
    geometry = f"{tire.width_mm}/{tire.profile_ratio} {const_code}{tire.diameter_inch}"
    parts.append(geometry)

    load_str = str(tire.load_index_code) if tire.load_index_code is not None else ""
    speed_str = tire.speed_index_code or ""
    if load_str or speed_str:
        parts.append(f"{load_str}{speed_str}")

    seasonality = tire.__dict__.get("seasonality")
    if seasonality is not None and seasonality.name:
        parts.append(seasonality.name.strip())

    technologies = tire.__dict__.get("technologies") or []
    if technologies:
        tech_codes = [t.code.strip() for t in technologies if t.code]
        if tech_codes:
            parts.append(" ".join(tech_codes))

    joined = " ".join(parts)
    if tire.sku and tire.sku.strip() not in joined:
        parts.append(tire.sku.strip())

    return " ".join(parts).strip()


def embed_passages(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """Генерирует нормализованные эмбеддинги каталожных позиций с префиксом 'passage: '."""
    if not texts:
        return []
    model = get_embedding_model()
    prefixed = [f"{PASSAGE_PREFIX}{t.strip()}" for t in texts]
    vectors = model.encode(
        prefixed,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [vec.tolist() for vec in vectors]


def embed_queries(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """Генерирует нормализованные эмбеддинги поисковых запросов прайса с префиксом 'query: '."""
    if not texts:
        return []
    model = get_embedding_model()
    prefixed = [f"{QUERY_PREFIX}{t.strip()}" for t in texts]
    vectors = model.encode(
        prefixed,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [vec.tolist() for vec in vectors]


def match_two_tables_in_memory(
    enterprise_rows: list[dict[str, Any]],
    supplier_rows: list[dict[str, Any]],
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Реализует прямой контракт со схемы 'Две таблицы + токен' -> 'Возврат сопоставленной номенклатуры':
    принимает таблицу номенклатуры предприятия и таблицу поставщика, векторизует обе и возвращает
    таблицу сопоставлений (предприятие, поставщик, цена поставщика, точность совпадения).
    """
    if not supplier_rows:
        return []
    if not enterprise_rows:
        return [
            {
                "row_number": idx + 1,
                "enterprise_id": None,
                "enterprise_name": None,
                "supplier_name": str(s.get("raw_name", "")),
                "supplier_price": float(s.get("price", 0.0)),
                "confidence_score": 0.0,
                "match_status": MatchStatusEnum.UNMATCHED.value,
                "candidates": [],
            }
            for idx, s in enumerate(supplier_rows)
        ]

    ent_texts = [str(e.get("search_text") or e.get("name") or e.get("sku") or "") for e in enterprise_rows]
    sup_texts = [str(s.get("raw_name") or s.get("name") or "") for s in supplier_rows]

    p_mat = np.asarray(embed_passages(ent_texts), dtype=float)  # (N_ent, D)
    q_mat = np.asarray(embed_queries(sup_texts), dtype=float)   # (N_sup, D)

    # Нормализуем на случай моков в тестах и вычисляем матрицу косинусной близости
    p_norms = np.linalg.norm(p_mat, axis=1, keepdims=True)
    q_norms = np.linalg.norm(q_mat, axis=1, keepdims=True)
    p_safe = np.divide(p_mat, p_norms, out=np.zeros_like(p_mat), where=p_norms > 0)
    q_safe = np.divide(q_mat, q_norms, out=np.zeros_like(q_mat), where=q_norms > 0)
    sim_matrix = np.clip(q_safe @ p_safe.T, 0.0, 1.0)

    k = min(max(1, top_k), len(enterprise_rows))
    matched_table: list[dict[str, Any]] = []

    for row_idx, sup_row in enumerate(supplier_rows):
        sims = sim_matrix[row_idx]
        top_indices = np.argsort(-sims)[:k]

        candidates = []
        for idx in top_indices:
            ent = enterprise_rows[int(idx)]
            ent_label = str(ent.get("name") or ent.get("sku") or ent.get("search_text") or "")
            candidates.append(
                {
                    "tire_id": ent.get("id"),
                    "sku": ent_label,
                    "score": round(float(sims[int(idx)]), 6),
                }
            )

        best = candidates[0]
        best_score = float(best["score"])
        if best_score >= settings.MATCH_AUTO_THRESHOLD:
            m_status = MatchStatusEnum.AUTO.value
            ent_id = best["tire_id"]
            ent_name = best["sku"]
        elif best_score >= settings.MATCH_REVIEW_THRESHOLD:
            m_status = MatchStatusEnum.NEEDS_REVIEW.value
            ent_id = best["tire_id"]
            ent_name = best["sku"]
        else:
            m_status = MatchStatusEnum.UNMATCHED.value
            ent_id = None
            ent_name = best["sku"]

        matched_table.append(
            {
                "row_number": row_idx + 1,
                "enterprise_id": ent_id,
                "enterprise_name": ent_name,
                "supplier_name": str(sup_row.get("raw_name") or sup_row.get("name") or ""),
                "supplier_price": float(sup_row.get("price", 0.0)),
                "confidence_score": round(best_score, 4),
                "match_status": m_status,
                "candidates": candidates,
            }
        )

    return matched_table


def build_knn_query(query_vec: list[float], top_k: int = 15) -> Select[tuple[Tire, Any]]:
    """Строит SQL-запрос k-NN поиска по косинусному расстоянию (<=>) в pgvector."""
    distance_col = Tire.embedding.cosine_distance(query_vec).label("distance")
    return (
        select(Tire, distance_col)
        .where(Tire.embedding.is_not(None))
        .order_by(distance_col)
        .limit(top_k)
    )


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


async def search_by_vector(
    db: AsyncSession,
    query_vec: list[float],
    top_k: int = 15,
) -> list[tuple[Tire, float]]:
    """Выполняет k-NN поиск ближайших шин по готовому вектору запроса."""
    bind = db.get_bind()
    dialect_name = bind.dialect.name if bind is not None else "postgresql"

    if dialect_name == "postgresql":
        await db.execute(text(f"SET hnsw.ef_search = {int(settings.HNSW_EF_SEARCH)}"))
        stmt = build_knn_query(query_vec=query_vec, top_k=top_k)
        result = await db.execute(stmt)
        rows = result.all()
        return [
            (tire, round(max(0.0, min(1.0, 1.0 - float(distance))), 6))
            for tire, distance in rows
        ]

    res = await db.execute(select(Tire).where(Tire.embedding.is_not(None)))
    tires = res.scalars().all()
    scored: list[tuple[Tire, float]] = []
    for tire in tires:
        if tire.embedding is not None:
            sim = _cosine_similarity(query_vec, list(tire.embedding))
            scored.append((tire, round(sim, 6)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:top_k]


async def search_top_candidates(
    db: AsyncSession,
    raw_name: str,
    top_k: int = 15,
) -> list[tuple[Tire, float]]:
    """Векторизует сырое наименование из прайс-листа и находит top_k кандидатов в каталоге Tire."""
    if not raw_name or not raw_name.strip():
        return []
    vectors = embed_queries([raw_name], batch_size=1)
    return await search_by_vector(db=db, query_vec=vectors[0], top_k=top_k)


async def search_top_candidates_batch(
    db: AsyncSession,
    names: list[str],
    top_k: int = 15,
) -> list[list[tuple[Tire, float]]]:
    """Пакетный поиск кандидатов для батча наименований."""
    if not names:
        return []
    query_vectors = embed_queries(names, batch_size=len(names))
    results: list[list[tuple[Tire, float]]] = []
    for vec in query_vectors:
        candidates = await search_by_vector(db=db, query_vec=vec, top_k=top_k)
        results.append(candidates)
    return results
