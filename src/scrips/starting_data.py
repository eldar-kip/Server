import random
from src.server.core.database import SessionLocal
from src.server.models import (
    Tire,
    SpeedIndex,
    LoadIndex,
    ConstructionType,
    Seasonality,
    TreadPattern,
    Technology,
)

def generate_50_tires():
    with SessionLocal() as db:
        print("⏳ Загружаем справочники из базы...")
        
        # Получаем все справочные данные из БД в виде списков
        seasons = db.query(Seasonality).all()
        patterns = db.query(TreadPattern).all()
        speeds = db.query(SpeedIndex).all()
        loads = db.query(LoadIndex).all()
        constructions = db.query(ConstructionType).all()
        technologies = db.query(Technology).all()

        if not seasons:
            print("❌ Ошибка: Сначала нужно заполнить справочники! Запустите предыдущий скрипт seed_db.py.")
            return

        print("🎲 Генерируем 50 случайных шин...")

        # Списки для генерации реалистичных названий и размеров
        brands = ["Michelin", "Pirelli", "Nokian", "Continental", "Bridgestone", "Goodyear", "Hankook", "Yokohama"]
        models = ["Sport Pro", "Winter Ice", "AllSeason Max", "Eco Drive", "Premium", "UltraGrip"]
        
        widths = [175, 185, 195, 205, 215, 225, 235, 245, 255, 265, 275]
        profiles = [40, 45, 50, 55, 60, 65, 70]
        diameters = [14, 15, 16, 17, 18, 19, 20, 21]

        new_tires: list[Tire] = []
        for _ in range(50):
            # 1. Выбираем случайные характеристики
            brand = random.choice(brands)
            model = random.choice(models)
            width = random.choice(widths)
            profile = random.choice(profiles)
            diameter = random.choice(diameters)
            
            speed = random.choice(speeds)
            load = random.choice(loads)
            const = random.choice(constructions)
            
            # Формируем красивый SKU (Артикул/Название)
            sku = f"{brand} {model} {width}/{profile} {const.code}{diameter} {load.code}{speed.code} (ID-{random.randint(1000, 9999)})"

            # 2. Создаем объект шины
            tire = Tire(
                sku=sku,
                width_mm=width,
                profile_ratio=profile,
                diameter_inch=diameter,
                construction_type_code=const.code,
                load_index_code=load.code,
                speed_index_code=speed.code,
                seasonality_id=random.choice(seasons).id,
                tread_pattern_id=random.choice(patterns).id
            )

            # 3. Добавляем технологии (с вероятностью 30% шина получит 1-2 технологии)
            if random.random() > 0.7 and technologies:
                # Берем от 1 до 2 случайных технологий
                techs_to_add = random.sample(technologies, k=random.randint(1, min(2, len(technologies))))
                for tech in techs_to_add:
                    tire.technologies.append(tech)

            new_tires.append(tire)

        # 4. Сохраняем все 50 шин в базу одним махом
        db.add_all(new_tires)
        db.commit()

        print(f"✅ Успешно добавлено {len(new_tires)} новых шин!")

if __name__ == "__main__":
    generate_50_tires()