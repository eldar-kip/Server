from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.server.models import (
    AuditActionEnum,
    ConstructionType,
    DemandItem,
    FeedbackAuditLog,
    LoadIndex,
    Manufacturer,
    MatchStatusEnum,
    OptimizationResult,
    Pattern,
    ProcurementSession,
    RawOfferItem,
    Seasonality,
    SpeedIndex,
    Supplier,
    TaskStatusEnum,
    Technology,
    Tire,
    TreadPattern,
    User,
)


async def test_orm_models_and_cascade_relations(db_session: AsyncSession, buyer_user: User) -> None:
    """Проверяет создание справочников, шин, сессий, связей и каскадное удаление."""
    speed_v = SpeedIndex(code="V", max_speed_kmh=240)
    load_91 = LoadIndex(code=91, max_load_kg=615)
    const_r = ConstructionType(code="R", name="Радиальная")
    season_summer = Seasonality(name="Летние")
    tread_asym = TreadPattern(name="Асимметричный")
    tech_rf = Technology(code="RunFlat", description="Движение при проколе")
    mfg = Manufacturer(name="Pirelli", country="Италия")
    supplier = Supplier(name="ООО ШинТорг", inn="7700001111")

    db_session.add_all([speed_v, load_91, const_r, season_summer, tread_asym, tech_rf, mfg, supplier])
    await db_session.flush()

    pattern = Pattern(name="P Zero", manufacturer_id=mfg.id, seasonality_id=season_summer.id)
    db_session.add(pattern)
    await db_session.flush()

    tire = Tire(
        sku="Pirelli P Zero 225/45 R17 91V RunFlat",
        width_mm=225,
        profile_ratio=45,
        diameter_inch=17,
        speed_index_code="V",
        load_index_code=91,
        construction_type_code="R",
        seasonality_id=season_summer.id,
        tread_pattern_id=tread_asym.id,
        pattern_id=pattern.id,
        search_text="Pirelli P Zero 225/45 R17 91V Летние RunFlat",
    )
    tire.technologies.append(tech_rf)
    db_session.add(tire)
    await db_session.flush()

    proc_session = ProcurementSession(
        user_id=buyer_user.id,
        title="Тестовая закупка",
        status=TaskStatusEnum.PENDING,
        total_items=1,
        matched_items=1,
    )
    db_session.add(proc_session)
    await db_session.flush()

    offer = RawOfferItem(
        session_id=proc_session.id,
        supplier_id=supplier.id,
        raw_name="Шина Pirelli 225/45R17 91V PZERO",
        price=9500.0,
        quantity=8,
        lot_size=4,
        min_order_qty=4,
        matched_tire_id=tire.id,
        confidence_score=0.92,
        match_status=MatchStatusEnum.AUTO,
        candidates=[{"tire_id": tire.id, "sku": tire.sku, "score": 0.92}],
    )
    demand = DemandItem(session_id=proc_session.id, tire_id=tire.id, required_qty=4)
    audit = FeedbackAuditLog(
        session_id=proc_session.id,
        user_id=buyer_user.id,
        action=AuditActionEnum.APPROVE,
    )
    opt = OptimizationResult(
        session_id=proc_session.id,
        total_cost=38000.0,
        baseline_cost=40000.0,
        savings=2000.0,
        savings_percent=5.0,
        solver_method="milp",
        allocations=[],
        unfulfilled=[],
    )
    db_session.add_all([offer, demand, audit, opt])
    await db_session.commit()

    loaded_tire = (
        await db_session.execute(
            select(Tire).where(Tire.id == tire.id).options(selectinload(Tire.technologies))
        )
    ).scalar_one()
    assert len(loaded_tire.technologies) == 1
    assert loaded_tire.technologies[0].code == "RunFlat"

    # Проверка каскадного удаления сессии
    await db_session.delete(proc_session)
    await db_session.commit()

    assert (await db_session.execute(select(RawOfferItem))).scalars().all() == []
    assert (await db_session.execute(select(DemandItem))).scalars().all() == []
    assert (await db_session.execute(select(FeedbackAuditLog))).scalars().all() == []
    assert (await db_session.execute(select(OptimizationResult))).scalars().all() == []


def test_alembic_migration_history_is_linear() -> None:
    """Проверяет целостность и линейность цепочки миграций Alembic без ветвлений."""
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert heads == ["d6f4a9b304e5"]

    revisions = [rev.revision for rev in script.walk_revisions()]
    assert revisions == [
        "d6f4a9b304e5",
        "c5d3e8a203d4",
        "b4e2d7f102c3",
        "a3f1c8d901b2",
        "2afb21c305de",
        "81359e44c8e4",
    ]


async def test_seed_scripts_and_build_embeddings_idempotency(
    db_session_factory,
) -> None:
    """Проверяет async-сидирование (seed_db, starting_data) и идемпотентный build_tire_embeddings."""
    from unittest.mock import patch
    from src.scripts import build_tire_embeddings as bte
    from src.scripts import seed_db, starting_data

    def _fake_embed_passages(texts: list[str], batch_size: int = 64) -> list[list[float]]:
        return [[0.01] * 1024 for _ in texts]

    with (
        patch.object(seed_db, "AsyncSessionLocal", db_session_factory),
        patch.object(starting_data, "AsyncSessionLocal", db_session_factory),
        patch.object(bte, "AsyncSessionLocal", db_session_factory),
        patch.object(bte, "embed_passages", side_effect=_fake_embed_passages),
    ):
        await seed_db.seed_database()
        # Повторный запуск идемпотентен
        await seed_db.seed_database()
        await starting_data.generate_50_tires()

        updated_first = await bte.build_embeddings(rebuild=False)
        assert updated_first == 53  # 3 из seed_db + 50 из starting_data

        # Повторный запуск без --rebuild ничего не пересчитывает
        updated_second = await bte.build_embeddings(rebuild=False)
        assert updated_second == 0

        # С флагом rebuild=True пересчитывает все
        updated_rebuild = await bte.build_embeddings(rebuild=True)
        assert updated_rebuild == 53
