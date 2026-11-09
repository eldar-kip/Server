import asyncio
from sqlalchemy import select

from src.ml_worker.vector_search import build_tire_search_text
from src.server.core.database import AsyncSessionLocal
from src.server.core.security import get_password_hash
from src.server.models import (
    ConstructionType,
    LoadIndex,
    Manufacturer,
    Pattern,
    RoleEnum,
    Seasonality,
    SpeedIndex,
    Supplier,
    Technology,
    Tire,
    TreadPattern,
    User,
)


async def seed_database() -> None:
    """Асинхронное идемпотентное заполнение БД справочниками, поставщиками, пользователями и шинами."""
    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(Seasonality).limit(1))
        if existing.scalars().first():
            print("База данных уже содержит справочники. Повторное заполнение пропущено.")
            return

        print("Начинаем заполнение справочников (схема должна быть создана через alembic upgrade head)...")

        const_r = ConstructionType(code="R", name="Радиальная")
        const_d = ConstructionType(code="D", name="Диагональная")

        season_summer = Seasonality(name="Летняя")
        season_winter = Seasonality(name="Зимняя")
        season_all = Seasonality(name="Всесезонная")

        tread_sym = TreadPattern(name="Симметричный")
        tread_asym = TreadPattern(name="Асимметричный")
        tread_dir = TreadPattern(name="Направленный")

        speed_h = SpeedIndex(code="H", max_speed_kmh=210)
        speed_v = SpeedIndex(code="V", max_speed_kmh=240)
        speed_w = SpeedIndex(code="W", max_speed_kmh=270)

        load_91 = LoadIndex(code=91, max_load_kg=615)
        load_94 = LoadIndex(code=94, max_load_kg=670)
        load_100 = LoadIndex(code=100, max_load_kg=800)

        tech_runflat = Technology(code="RunFlat", description="Технология сохранения мобильности при проколе")
        tech_xl = Technology(code="XL", description="Extra Load (усиленная боковина)")
        tech_stud = Technology(code="Studded", description="Шипованная шина")

        mfg_michelin = Manufacturer(name="Michelin", country="Франция")
        mfg_pirelli = Manufacturer(name="Pirelli", country="Италия")
        mfg_nokian = Manufacturer(name="Nokian", country="Финляндия")
        mfg_bridgestone = Manufacturer(name="Bridgestone", country="Япония")

        sup_1 = Supplier(name="ООО АвтоШина Опт", inn="7701234567")
        sup_2 = Supplier(name="АО ЕвроДиск Логистик", inn="7809876543")

        db.add_all(
            [
                const_r,
                const_d,
                season_summer,
                season_winter,
                season_all,
                tread_sym,
                tread_asym,
                tread_dir,
                speed_h,
                speed_v,
                speed_w,
                load_91,
                load_94,
                load_100,
                tech_runflat,
                tech_xl,
                tech_stud,
                mfg_michelin,
                mfg_pirelli,
                mfg_nokian,
                mfg_bridgestone,
                sup_1,
                sup_2,
            ]
        )
        await db.flush()

        pat_ps4 = Pattern(manufacturer_id=mfg_michelin.id, name="Pilot Sport 4", seasonality_id=season_summer.id)
        pat_pzero = Pattern(manufacturer_id=mfg_pirelli.id, name="P Zero", seasonality_id=season_summer.id)
        pat_hakka10 = Pattern(manufacturer_id=mfg_nokian.id, name="Hakkapeliitta 10", seasonality_id=season_winter.id)
        pat_t005 = Pattern(manufacturer_id=mfg_bridgestone.id, name="Turanza T005", seasonality_id=season_summer.id)
        db.add_all([pat_ps4, pat_pzero, pat_hakka10, pat_t005])
        await db.flush()

        tire_1 = Tire(
            sku="Michelin Pilot Sport 4 225/45 R17 94W XL",
            width_mm=225,
            profile_ratio=45,
            diameter_inch=17,
            construction_type_code=const_r.code,
            load_index_code=load_94.code,
            speed_index_code=speed_w.code,
            seasonality_id=season_summer.id,
            tread_pattern_id=tread_asym.id,
            pattern_id=pat_ps4.id,
        )
        tire_1.pattern = pat_ps4
        pat_ps4.manufacturer = mfg_michelin
        tire_1.seasonality = season_summer
        tire_1.technologies.append(tech_xl)
        tire_1.search_text = build_tire_search_text(tire_1)

        tire_2 = Tire(
            sku="Nokian Hakkapeliitta 10 205/55 R16 91H RunFlat",
            width_mm=205,
            profile_ratio=55,
            diameter_inch=16,
            construction_type_code=const_r.code,
            load_index_code=load_91.code,
            speed_index_code=speed_h.code,
            seasonality_id=season_winter.id,
            tread_pattern_id=tread_dir.id,
            pattern_id=pat_hakka10.id,
        )
        tire_2.pattern = pat_hakka10
        pat_hakka10.manufacturer = mfg_nokian
        tire_2.seasonality = season_winter
        tire_2.technologies.extend([tech_runflat, tech_stud])
        tire_2.search_text = build_tire_search_text(tire_2)

        tire_3 = Tire(
            sku="Pirelli P Zero 225/45 R17 91V",
            width_mm=225,
            profile_ratio=45,
            diameter_inch=17,
            construction_type_code=const_r.code,
            load_index_code=load_91.code,
            speed_index_code=speed_v.code,
            seasonality_id=season_summer.id,
            tread_pattern_id=tread_asym.id,
            pattern_id=pat_pzero.id,
        )
        tire_3.pattern = pat_pzero
        pat_pzero.manufacturer = mfg_pirelli
        tire_3.seasonality = season_summer
        tire_3.search_text = build_tire_search_text(tire_3)

        db.add_all([tire_1, tire_2, tire_3])

        user_res = await db.execute(select(User).where(User.email == "admin@taires.com"))
        if not user_res.scalars().first():
            db.add_all(
                [
                    User(
                        email="admin@taires.com",
                        hashed_password=get_password_hash("admin123"),
                        role=RoleEnum.ADMIN,
                    ),
                    User(
                        email="buyer@taires.com",
                        hashed_password=get_password_hash("buyer123"),
                        role=RoleEnum.BUYER,
                    ),
                ]
            )

        await db.commit()
        print("Сидирование БД успешно завершено (справочники, шины, поставщики, пользователи).")


if __name__ == "__main__":
    asyncio.run(seed_database())
