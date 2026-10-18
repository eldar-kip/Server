import asyncio
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from src.server.core.database import AsyncSessionLocal, engine
from src.server.core.security import get_password_hash
from src.server.models import (
    Base,
    ConstructionType,
    LoadIndex,
    Manufacturer,
    Pattern,
    ProcurementSession,
    RawOfferItem,
    RoleEnum,
    Seasonality,
    SpeedIndex,
    TaskStatusEnum,
    Technology,
    Tire,
    TreadPattern,
    User,
)


async def seed_database():
    print("⏳ 1. Проверяем таблицы в базе данных...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Таблицы готовы!")

    async with AsyncSessionLocal() as db:
        try:
            print("\n⏳ 2. Заполняем справочники и тестовые данные...")

            # --- 2.1 Справочники ---
            speed_v = SpeedIndex(code="V", max_speed_kmh=240)
            speed_w = SpeedIndex(code="W", max_speed_kmh=270)
            load_91 = LoadIndex(code=91, max_load_kg=615)
            load_94 = LoadIndex(code=94, max_load_kg=670)
            const_r = ConstructionType(code="R", name="Радиальная")
            
            season_summer = Seasonality(id=1, name="Летние")
            season_winter = Seasonality(id=2, name="Зимние (нешипованные)")
            season_spikes = Seasonality(id=3, name="Зимние (шипованные)")
            
            tread_asym = TreadPattern(id=1, name="Асимметричный")
            tread_sym = TreadPattern(id=2, name="Симметричный")
            
            tech_runflat = Technology(id=1, code="RunFlat", description="Движение при проколе")
            tech_xl = Technology(id=2, code="XL", description="Усиленная боковина")
            tech_seal = Technology(id=3, code="SealInside", description="Самозатягивание проколов")

            db.add_all([
                speed_v, speed_w,
                load_91, load_94,
                const_r,
                season_summer, season_winter, season_spikes,
                tread_asym, tread_sym,
                tech_runflat, tech_xl, tech_seal,
            ])
            await db.flush()

            # --- 2.2 Производители и модели (Patterns) ---
            pirelli = Manufacturer(id=1, name="Pirelli", country="Италия")
            michelin = Manufacturer(id=2, name="Michelin", country="Франция")
            db.add_all([pirelli, michelin])
            await db.flush()

            pzero = Pattern(
                id=1,
                name="P Zero",
                manufacturer_id=pirelli.id,
                seasonality_id=season_summer.id,
            )
            cinturato = Pattern(
                id=2,
                name="Cinturato P7",
                manufacturer_id=pirelli.id,
                seasonality_id=season_summer.id,
            )
            db.add_all([pzero, cinturato])
            await db.flush()

            # --- 2.3 Номенклатура шин ---
            tire_check = await db.execute(select(Tire).filter_by(sku="Pirelli P Zero 225/45 R17 91V RunFlat"))
            if not tire_check.scalars().first():
                new_tire = Tire(
                    sku="Pirelli P Zero 225/45 R17 91V RunFlat",
                    width_mm=225,
                    profile_ratio=45,
                    diameter_inch=17,
                    speed_index_code="V",
                    load_index_code=91,
                    construction_type_code="R",
                    seasonality_id=season_summer.id,
                    tread_pattern_id=tread_asym.id,
                    pattern_id=pzero.id,
                )
                new_tire.technologies.extend([tech_runflat, tech_xl])
                db.add(new_tire)

            # --- 2.4 Пользователи ---
            user_check = await db.execute(select(User).filter_by(email="admin@taires.com"))
            if not user_check.scalars().first():
                admin_user = User(
                    email="admin@taires.com",
                    hashed_password=get_password_hash("admin123"),
                    role=RoleEnum.ADMIN,
                )
                buyer_user = User(
                    email="buyer@taires.com",
                    hashed_password=get_password_hash("buyer123"),
                    role=RoleEnum.BUYER,
                )
                db.add_all([admin_user, buyer_user])

            # --- 2.5 Закупочная сессия и офферы ---
            session_check = await db.execute(select(ProcurementSession).limit(1))
            if not session_check.scalars().first():
                proc_session = ProcurementSession(
                    title="Закупка Лето 2026 - Поставщик АвтоШина",
                    status=TaskStatusEnum.PENDING,
                    total_items=2,
                    matched_items=0,
                )
                item1 = RawOfferItem(
                    raw_name="Шина Pirelli 225/45R17 91V PZERO R-F",
                    price=9500.0,
                    quantity=8,
                    supplier_article="PIR-2254517-PZ",
                    session=proc_session,
                )
                item2 = RawOfferItem(
                    raw_name="Шина Michelin Pilot Sport 4 205/55R16",
                    price=8200.0,
                    quantity=12,
                    supplier_article="MICH-PS4-16",
                    session=proc_session,
                )
                db.add_all([proc_session, item1, item2])

            await db.commit()
            print("✅ Все тестовые данные успешно сохранены в PostgreSQL!")

            # --- 3. Читаем и выводим сохранённые данные ---
            print("\n⏳ 3. Проверка чтения сохранённых данных из БД...")

            # Читаем шину с связями
            stmt_tire = select(Tire).options(
                selectinload(Tire.speed_index),
                selectinload(Tire.load_index),
                selectinload(Tire.pattern).selectinload(Pattern.manufacturer),
                selectinload(Tire.seasonality),
                selectinload(Tire.technologies),
            )
            res_tire = await db.execute(stmt_tire)
            saved_tire = res_tire.scalars().first()

            if saved_tire:
                print("\n📦 --- ШИНА В КАТАЛОГЕ ---")
                print(f"SKU: {saved_tire.sku}")
                print(f"Размерность: {saved_tire.width_mm}/{saved_tire.profile_ratio} R{saved_tire.diameter_inch}")
                print(f"Производитель: {saved_tire.pattern.manufacturer.name if saved_tire.pattern else 'N/A'}")
                print(f"Модель: {saved_tire.pattern.name if saved_tire.pattern else 'N/A'}")
                print(f"Сезонность: {saved_tire.seasonality.name if saved_tire.seasonality else 'N/A'}")
                print(f"Индекс скорости: {saved_tire.speed_index.code} (до {saved_tire.speed_index.max_speed_kmh} км/ч)")
                print(f"Индекс нагрузки: {saved_tire.load_index.code} (до {saved_tire.load_index.max_load_kg} кг)")
                tech_list = [t.code for t in saved_tire.technologies]
                print(f"Технологии: {', '.join(tech_list)}")

            # Читаем пользователей
            res_users = await db.execute(select(User))
            users = res_users.scalars().all()
            print("\n👥 --- ПОЛЬЗОВАТЕЛИ ---")
            for u in users:
                print(f"ID: {u.id} | Email: {u.email} | Роль: {u.role.value}")

            # Читаем сессии закупок
            stmt_sess = select(ProcurementSession).options(selectinload(ProcurementSession.raw_offer_items))
            res_sess = await db.execute(stmt_sess)
            sessions = res_sess.scalars().all()
            print("\n📋 --- ЗАКУПОЧНЫЕ СЕССИИ ---")
            for s in sessions:
                print(f"Сессия #{s.id}: '{s.title}' | Статус: {s.status.value}")
                for it in s.raw_offer_items:
                    print(f"   -> Оффер: {it.raw_name} | Цена: {it.price} ₽ | Кол-во: {it.quantity} шт.")

        except Exception as e:
            print(f"❌ Произошла ошибка: {e}")
            await db.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(seed_database())