import asyncio
import random
from sqlalchemy import select

from src.ml_worker.vector_search import build_tire_search_text
from src.server.core.database import AsyncSessionLocal
from src.server.models import (
    ConstructionType,
    LoadIndex,
    Manufacturer,
    Pattern,
    Seasonality,
    SpeedIndex,
    Technology,
    Tire,
    TreadPattern,
)


async def generate_50_tires() -> None:
    """Асинхронная генерация 50 реалистичных позиций каталога шин."""
    async with AsyncSessionLocal() as db:
        seasons = list((await db.execute(select(Seasonality))).scalars().all())
        treads = list((await db.execute(select(TreadPattern))).scalars().all())
        speeds = list((await db.execute(select(SpeedIndex))).scalars().all())
        loads = list((await db.execute(select(LoadIndex))).scalars().all())
        constructions = list((await db.execute(select(ConstructionType))).scalars().all())
        technologies = list((await db.execute(select(Technology))).scalars().all())
        manufacturers = list((await db.execute(select(Manufacturer))).scalars().all())

        if not seasons or not speeds or not loads or not constructions:
            print("Ошибка: сначала запустите сидирование справочников (python -m src.scripts.seed_db).")
            return

        brand_names = ["Michelin", "Pirelli", "Nokian", "Continental", "Bridgestone", "Goodyear", "Hankook", "Yokohama"]
        mfg_by_name = {m.name: m for m in manufacturers}
        for b_name in brand_names:
            if b_name not in mfg_by_name:
                m = Manufacturer(name=b_name)
                db.add(m)
                mfg_by_name[b_name] = m
        await db.flush()

        model_names = ["Sport Pro", "Winter Ice", "AllSeason Max", "Eco Drive", "Premium", "UltraGrip"]
        widths = [175, 185, 195, 205, 215, 225, 235, 245, 255, 265, 275]
        profiles = [40, 45, 50, 55, 60, 65, 70]
        diameters = [14, 15, 16, 17, 18, 19, 20, 21]

        new_tires: list[Tire] = []
        for idx in range(50):
            brand = random.choice(brand_names)
            model_title = random.choice(model_names)
            season = random.choice(seasons)
            mfg = mfg_by_name[brand]

            pattern = Pattern(
                manufacturer_id=mfg.id,
                name=model_title,
                seasonality_id=season.id,
            )
            pattern.manufacturer = mfg
            db.add(pattern)
            await db.flush()

            width = random.choice(widths)
            profile = random.choice(profiles)
            diameter = random.choice(diameters)
            speed = random.choice(speeds)
            load = random.choice(loads)
            const = random.choice(constructions)

            sku = f"{brand} {model_title} {width}/{profile} {const.code}{diameter} {load.code}{speed.code} #{random.randint(1000, 9999)}-{idx}"

            tire = Tire(
                sku=sku,
                width_mm=width,
                profile_ratio=profile,
                diameter_inch=diameter,
                construction_type_code=const.code,
                load_index_code=load.code,
                speed_index_code=speed.code,
                seasonality_id=season.id,
                tread_pattern_id=random.choice(treads).id if treads else None,
                pattern_id=pattern.id,
            )
            tire.pattern = pattern
            tire.seasonality = season

            if random.random() > 0.7 and technologies:
                techs_to_add = random.sample(technologies, k=random.randint(1, min(2, len(technologies))))
                tire.technologies.extend(techs_to_add)

            tire.search_text = build_tire_search_text(tire)
            db.add(tire)
            new_tires.append(tire)

        await db.commit()
        print(f"Успешно добавлено {len(new_tires)} новых шин!")


if __name__ == "__main__":
    asyncio.run(generate_50_tires())
