import asyncio
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from server.core.database import AsyncSessionLocal, engine
from server.models import Base, ConstructionType, LoadIndex, SpeedIndex, Technology, Tire

async def run_test():
    print("⏳ 1. Создаем таблицы в базе данных (если их нет)...")
    # Асинхронное создание таблиц требует запуска синхронного метода внутри соединения
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Таблицы созданы!")

    # Открываем асинхронную сессию
    async with AsyncSessionLocal() as db:
        try:
            print("\n⏳ 2. Заполняем справочники и добавляем шину...")
            
            # Проверяем, пустая ли база
            result = await db.execute(select(Tire).limit(1))
            existing_tire = result.scalars().first()

            if not existing_tire:
                speed_v = SpeedIndex(code="V", max_speed_kmh=240)
                load_91 = LoadIndex(code=91, max_load_kg=615)
                const_r = ConstructionType(code="R", name="Радиальная")
                
                tech_runflat = Technology(code="RunFlat", description="Движение при проколе")
                tech_xl = Technology(code="XL", description="Усиленная боковина")

                new_tire = Tire(
                    sku="Pirelli P Zero 225/45 R17 91V RunFlat",
                    width_mm=225,
                    profile_ratio=45,
                    diameter_inch=17,
                    speed_index=speed_v,
                    load_index=load_91,
                    construction_type=const_r
                )
                
                # Добавляем технологии
                new_tire.technologies.extend([tech_runflat, tech_xl])

                db.add(new_tire)
                await db.commit()
                print("✅ Тестовые данные успешно сохранены!")
            else:
                print("ℹ️ Данные уже есть в базе, пропускаем добавление.")

            print("\n⏳ 3. Читаем данные обратно из БД...")
            
            # В асинхронном SQLAlchemy связи нужно подгружать явно через selectinload
            stmt = select(Tire).options(
                selectinload(Tire.speed_index),
                selectinload(Tire.load_index),
                selectinload(Tire.technologies)
            )
            result = await db.execute(stmt)
            saved_tire = result.scalars().first()
            
            if saved_tire:
                print(f"\n--- РЕЗУЛЬТАТ ИЗ БАЗЫ ---")
                print(f"Товар: {saved_tire.sku}")
                print(f"Размер: {saved_tire.width_mm}/{saved_tire.profile_ratio} R{saved_tire.diameter_inch}")
                
                print(f"Индекс скорости: {saved_tire.speed_index.code} (до {saved_tire.speed_index.max_speed_kmh} км/ч)")
                print(f"Индекс нагрузки: {saved_tire.load_index.code} (до {saved_tire.load_index.max_load_kg} кг)")
                
                techs = [tech.code for tech in saved_tire.technologies]
                print(f"Примененные технологии: {', '.join(techs)}")
                print("-------------------------")

        except Exception as e:
            print(f"❌ Произошла ошибка: {e}")
            await db.rollback()

if __name__ == "__main__":
    # Запускаем асинхронную функцию через event loop
    asyncio.run(run_test())