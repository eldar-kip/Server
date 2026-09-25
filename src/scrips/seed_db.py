from src.server.core.database import SessionLocal, engine
from src.server.models import (
    Base,
    Tire,
    SpeedIndex,
    LoadIndex,
    ConstructionType,
    Seasonality,
    TreadPattern,
    Technology,
)

def seed_database():
    # 1. На всякий случай убеждаемся, что таблицы существуют
    Base.metadata.create_all(bind=engine)
    
    # 2. Открываем сессию (используем менеджер контекста with, чтобы она сама закрылась)
    with SessionLocal() as db:
        
        # Проверяем, есть ли уже данные в базе (например, по наличию сезонов)
        if db.query(Seasonality).first():
            print("ℹ️ База данных уже заполнена! Скрипт остановлен, чтобы избежать дублей.")
            return

        print("⏳ Начинаем заполнение базы данных справочниками...")

        # --- ЗАПОЛНЕНИЕ СПРАВОЧНИКОВ ---
        
        # Конструкции
        const_r = ConstructionType(code="R", name="Радиальная")
        const_d = ConstructionType(code="D", name="Диагональная")
        
        # Сезонность
        season_summer = Seasonality(name="Летняя")
        season_winter = Seasonality(name="Зимняя")
        season_all = Seasonality(name="Всесезонная")

        # Типы протектора
        tread_sym = TreadPattern(name="Симметричный")
        tread_asym = TreadPattern(name="Асимметричный")
        tread_dir = TreadPattern(name="Направленный")

        # Индексы скорости
        speed_h = SpeedIndex(code="H", max_speed_kmh=210)
        speed_v = SpeedIndex(code="V", max_speed_kmh=240)
        speed_w = SpeedIndex(code="W", max_speed_kmh=270)

        # Индексы нагрузки
        load_91 = LoadIndex(code=91, max_load_kg=615)
        load_94 = LoadIndex(code=94, max_load_kg=670)
        load_100 = LoadIndex(code=100, max_load_kg=800)

        # Технологии
        tech_runflat = Technology(code="RunFlat", description="Технология сохранения мобильности при проколе")
        tech_xl = Technology(code="XL", description="Extra Load (Усиленная боковина)")
        tech_stud = Technology(code="Studded", description="Шипованная шина")

        # Добавляем все справочники в сессию пакетом
        db.add_all([
            const_r, const_d,
            season_summer, season_winter, season_all,
            tread_sym, tread_asym, tread_dir,
            speed_h, speed_v, speed_w,
            load_91, load_94, load_100,
            tech_runflat, tech_xl, tech_stud
        ])
        
        # Делаем промежуточный коммит, чтобы получить ID для справочников
        db.commit()
        print("✅ Справочники успешно загружены!")

        print("⏳ Добавляем номенклатуру шин...")

        # --- ДОБАВЛЕНИЕ ШИН ---

        # Шина 1: Летняя премиальная (Асимметричная, усиленная)
        tire_1 = Tire(
            sku="Michelin Pilot Sport 4 225/45 R17 94W XL",
            width_mm=225,
            profile_ratio=45,
            diameter_inch=17,
            construction_type_code=const_r.code,
            load_index_code=load_94.code,
            speed_index_code=speed_w.code,
            seasonality_id=season_summer.id,
            tread_pattern_id=tread_asym.id
        )
        # Добавляем связь многие-ко-многим (Технология XL)
        tire_1.technologies.append(tech_xl)

        # Шина 2: Зимняя суровая (Направленная, шипы, RunFlat)
        tire_2 = Tire(
            sku="Nokian Hakkapeliitta 10 205/55 R16 91H RunFlat",
            width_mm=205,
            profile_ratio=55,
            diameter_inch=16,
            construction_type_code=const_r.code,
            load_index_code=load_91.code,
            speed_index_code=speed_h.code,
            seasonality_id=season_winter.id,
            tread_pattern_id=tread_dir.id
        )
        # Добавляем сразу две технологии
        tire_2.technologies.append(tech_runflat)
        tire_2.technologies.append(tech_stud)

        # Сохраняем шины
        db.add_all([tire_1, tire_2])
        db.commit()

        print("✅ Номенклатура шин успешно добавлена!")
        print("🚀 Сидинг базы данных полностью завершен.")

if __name__ == "__main__":
    seed_database()