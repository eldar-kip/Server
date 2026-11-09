import argparse
import asyncio
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.ml_worker.vector_search import build_tire_search_text, embed_passages
from src.server.core.database import AsyncSessionLocal
from src.server.models.references import Pattern
from src.server.models.tire import Tire

BATCH_SIZE = 64


async def build_embeddings(rebuild: bool = False) -> int:
    """
    Собирает search_text и рассчитывает эмбеддинги (multilingual-e5-large, 1024d)
    для всех шин каталога идемпотентно (только где embedding IS NULL, либо все при --rebuild).
    """
    async with AsyncSessionLocal() as db:
        stmt = select(Tire).options(
            selectinload(Tire.pattern).selectinload(Pattern.manufacturer),
            selectinload(Tire.seasonality),
            selectinload(Tire.technologies),
        )
        if not rebuild:
            stmt = stmt.where(Tire.embedding.is_(None))
        stmt = stmt.order_by(Tire.id)

        res = await db.execute(stmt)
        tires = list(res.scalars().all())
        if not tires:
            print("Все позиции каталога Tire уже векторизованы. Нечего обновлять.")
            return 0

        print(f"Найдено шин для векторизации: {len(tires)} (батч={BATCH_SIZE}).")
        updated = 0
        for start in range(0, len(tires), BATCH_SIZE):
            batch = tires[start : start + BATCH_SIZE]
            texts = [build_tire_search_text(t) for t in batch]
            vectors = embed_passages(texts, batch_size=BATCH_SIZE)

            for tire, s_text, vec in zip(batch, texts, vectors):
                tire.search_text = s_text
                tire.embedding = vec

            await db.commit()
            updated += len(batch)
            print(f"Обработано {updated}/{len(tires)} позиций каталога...")

        print(f"Готово! Эмбеддинги сохранены для {updated} шин.")
        return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Генерация эмбеддингов каталога шин Tire для pgvector")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Пересчитать эмбеддинги для всех шин (включая уже заполненные)",
    )
    args = parser.parse_args()
    asyncio.run(build_embeddings(rebuild=args.rebuild))


if __name__ == "__main__":
    main()
