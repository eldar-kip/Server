from server.core.database import SessionLocal, engine
from server.models import Base, ConstructionType, LoadIndex, SpeedIndex, Technology, Tire


def run_test():
    print("⏳ 1. Создаем таблицы в базе данных (если их нет)...")
    # Эта команда берет метаданные из нашего __init__.py и строит схему
    Base.metadata.create_all(bind=engine)
    print("✅ Таблицы созданы!")

    # Открываем сессию для работы с БД
    db = SessionLocal()

    try:
        print("\n⏳ 2. Заполняем справочники и добавляем шину...")
        
        # Проверяем, пустая ли база (чтобы не добавлять дубли при повторном запуске)
        if not db.query(Tire).first():
            # 2.1. Создаем объекты справочников
            speed_v = SpeedIndex(code="V", max_speed_kmh=240)
            load_91 = LoadIndex(code=91, max_load_kg=615)
            const_r = ConstructionType(code="R", name="Радиальная")
            
            # 2.2. Создаем парочку технологий
            tech_runflat = Technology(code="RunFlat", description="Движение при проколе")
            tech_xl = Technology(code="XL", description="Усиленная боковина")

            # 2.3. Создаем саму шину
            new_tire = Tire(
                sku="Pirelli P Zero 225/45 R17 91V RunFlat",
                width_mm=225,
                profile_ratio=45,
                diameter_inch=17,
                # Привязываем справочники прямо как объекты Python!
                speed_index=speed_v,
                load_index=load_91,
                construction_type=const_r
            )
            
            # 2.4. Магия SQLAlchemy для связи "Многие-ко-многим"
            # Мы просто добавляем технологии в список, а ORM сама заполнит промежуточную таблицу!
            new_tire.technologies.append(tech_runflat)
            new_tire.technologies.append(tech_xl)

            # Сохраняем всё в базу
            db.add(new_tire)
            db.commit()
            print("✅ Тестовые данные успешно сохранены!")
        else:
            print("ℹ️ Данные уже есть в базе, пропускаем добавление.")

        print("\n⏳ 3. Читаем данные обратно из БД...")
        # Достаем самую первую шину из базы
        saved_tire = db.query(Tire).first()
        
        # Выводим результат
        print(f"\n--- РЕЗУЛЬТАТ ИЗ БАЗЫ ---")
        assert saved_tire is not None
        print(f"Товар: {saved_tire.sku}")
        print(f"Размер: {saved_tire.width_mm}/{saved_tire.profile_ratio} R{saved_tire.diameter_inch}")
        
        # Обращаемся к связанным таблицам
        assert saved_tire.speed_index is not None
        print(f"Индекс скорости: {saved_tire.speed_index.code} (до {saved_tire.speed_index.max_speed_kmh} км/ч)")
        assert saved_tire.load_index is not None
        print(f"Индекс нагрузки: {saved_tire.load_index.code} (до {saved_tire.load_index.max_load_kg} кг)")
        
        # Достаем список технологий через связь многие-ко-многим
        techs = [tech.code for tech in saved_tire.technologies]
        print(f"Примененные технологии: {', '.join(techs)}")
        print("-------------------------")

    except Exception as e:
        print(f"❌ Произошла ошибка: {e}")
        db.rollback()
    finally:
        # Всегда закрываем сессию
        db.close()

if __name__ == "__main__":
    run_test()