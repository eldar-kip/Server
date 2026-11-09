import json
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.ml_worker import vector_search
from src.ml_worker.vector_search import (
    build_knn_query,
    build_tire_search_text,
    embed_passages,
    embed_queries,
    search_top_candidates,
)
from src.server.models import (
    Manufacturer,
    MatchStatusEnum,
    Pattern,
    ProcurementSession,
    RawOfferItem,
    Seasonality,
    TaskStatusEnum,
    Technology,
    Tire,
)
from src.server.tasks.matching_tasks import _process_session_in_db


def _basis_vec(index: int, dim: int = 1024) -> list[float]:
    vec = [0.0] * dim
    vec[index] = 1.0
    return vec


def test_build_tire_search_text_includes_structured_attributes() -> None:
    mfg = Manufacturer(name="Michelin")
    season = Seasonality(name="Летняя")
    pattern = Pattern(name="Pilot Sport 4")
    pattern.manufacturer = mfg

    tire = Tire(
        sku="MIC-PS4-2254517",
        width_mm=225,
        profile_ratio=45,
        diameter_inch=17,
        construction_type_code="R",
        load_index_code=94,
        speed_index_code="W",
    )
    tire.pattern = pattern
    tire.seasonality = season
    tire.technologies = [Technology(code="XL"), Technology(code="RunFlat")]

    text = build_tire_search_text(tire)
    assert "Michelin" in text
    assert "Pilot Sport 4" in text
    assert "225/45 R17" in text
    assert "94W" in text
    assert "Летняя" in text
    assert "XL RunFlat" in text
    assert "MIC-PS4-2254517" in text


def test_e5_prefixes_and_normalization() -> None:
    fake_model = MagicMock()
    fake_model.encode.return_value = np.ones((2, 1024), dtype=float)

    with patch.object(vector_search, "get_embedding_model", return_value=fake_model):
        p_vecs = embed_passages(["Шина A", "Шина B"], batch_size=16)
        assert len(p_vecs) == 2
        fake_model.encode.assert_called_with(
            ["passage: Шина A", "passage: Шина B"],
            batch_size=16,
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        q_vecs = embed_queries(["Запрос 1", "Запрос 2"], batch_size=8)
        assert len(q_vecs) == 2
        fake_model.encode.assert_called_with(
            ["query: Запрос 1", "query: Запрос 2"],
            batch_size=8,
            normalize_embeddings=True,
            show_progress_bar=False,
        )


def test_build_knn_query_compiles_pgvector_cosine_operator() -> None:
    stmt = build_knn_query(_basis_vec(0), top_k=15)
    compiled_sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "<=>" in compiled_sql
    assert "LIMIT" in compiled_sql.upper()


async def test_knn_search_with_mock_embeddings(db_session: AsyncSession) -> None:
    t1 = Tire(sku="Pirelli P Zero 225/45 R17", width_mm=225, profile_ratio=45, diameter_inch=17, embedding=_basis_vec(0))
    t2 = Tire(sku="Michelin PS4 205/55 R16", width_mm=205, profile_ratio=55, diameter_inch=16, embedding=_basis_vec(1))
    t3 = Tire(sku="Nokian Hakka 10 215/60 R16", width_mm=215, profile_ratio=60, diameter_inch=16, embedding=_basis_vec(2))
    db_session.add_all([t1, t2, t3])
    await db_session.commit()

    # Вектор запроса близок к t2 (cos=0.96) и чуть к t1 (cos=0.28)
    q_vec = [0.0] * 1024
    q_vec[0] = 0.28
    q_vec[1] = 0.96

    with patch.object(vector_search, "embed_queries", return_value=[q_vec]):
        candidates = await search_top_candidates(db_session, "Michelin 205/55 R16", top_k=2)

    assert len(candidates) == 2
    assert candidates[0][0].sku == "Michelin PS4 205/55 R16"
    assert candidates[0][1] == pytest.approx(0.96, abs=1e-3)
    assert candidates[1][0].sku == "Pirelli P Zero 225/45 R17"
    assert candidates[1][1] == pytest.approx(0.28, abs=1e-3)


async def test_matching_task_statuses_and_failed_progress(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проверяет батчевый матчинг (AUTO / NEEDS_REVIEW / UNMATCHED) и реальные счетчики при FAILED."""
    async with db_session_factory() as db:
        tire_a = Tire(sku="Tire AUTO", width_mm=205, profile_ratio=55, diameter_inch=16, embedding=_basis_vec(0))
        tire_b = Tire(sku="Tire REVIEW", width_mm=225, profile_ratio=45, diameter_inch=17, embedding=_basis_vec(1))
        db.add_all([tire_a, tire_b])
        await db.flush()

        sess = ProcurementSession(title="Прайс", status=TaskStatusEnum.PENDING, total_items=3)
        db.add(sess)
        await db.flush()
        sess_id = sess.id

        db.add_all(
            [
                RawOfferItem(session_id=sess_id, raw_name="Auto match item", price=5000, quantity=4),
                RawOfferItem(session_id=sess_id, raw_name="Review match item", price=6000, quantity=4),
                RawOfferItem(session_id=sess_id, raw_name="Unmatched item", price=7000, quantity=4),
            ]
        )
        await db.commit()

    # Три вектора: cos=1.0 (AUTO), cos=0.78 (NEEDS_REVIEW), cos=0.20 (UNMATCHED)
    v_auto = _basis_vec(0)
    v_review = [0.0] * 1024
    v_review[1] = 0.78
    v_review[3] = (1.0 - 0.78**2) ** 0.5
    v_unmatched = [0.0] * 1024
    v_unmatched[0] = 0.20
    v_unmatched[4] = (1.0 - 0.20**2) ** 0.5

    published: list[dict[str, object]] = []

    def _fake_publish(channel: str, message: str) -> None:
        published.append(json.loads(message))

    with (
        patch("src.server.tasks.matching_tasks.redis_client.publish", side_effect=_fake_publish),
        patch.object(vector_search, "embed_queries", return_value=[v_auto, v_review, v_unmatched]),
    ):
        await _process_session_in_db(session_id=sess_id, session_factory=db_session_factory)

    async with db_session_factory() as db:
        finished = (await db.execute(select(ProcurementSession).where(ProcurementSession.id == sess_id))).scalar_one()
        items = list(
            (await db.execute(select(RawOfferItem).where(RawOfferItem.session_id == sess_id).order_by(RawOfferItem.id)))
            .scalars()
            .all()
        )

    assert finished.status == TaskStatusEnum.COMPLETED
    assert finished.matched_items == 1  # только AUTO
    assert items[0].match_status == MatchStatusEnum.AUTO
    assert items[1].match_status == MatchStatusEnum.NEEDS_REVIEW
    assert items[2].match_status == MatchStatusEnum.UNMATCHED
    assert items[2].matched_tire_id is None
    assert published[-1]["status"] == "COMPLETED"
    assert published[-1]["message"] == "Обработано 3 из 3 позиций"

    # Проверка публикации реального total_items при сбое (FAILED)
    published.clear()
    with (
        patch("src.server.tasks.matching_tasks.redis_client.publish", side_effect=_fake_publish),
        patch(
            "src.server.tasks.matching_tasks.search_top_candidates_batch",
            side_effect=RuntimeError("ML error"),
        ),
    ):
        with pytest.raises(RuntimeError):
            await _process_session_in_db(session_id=sess_id, session_factory=db_session_factory)

    assert published[-1]["status"] == "FAILED"
    assert published[-1]["total_items"] == 3
