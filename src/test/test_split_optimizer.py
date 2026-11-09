from unittest.mock import patch
from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.server.models import (
    MatchStatusEnum,
    ProcurementSession,
    RawOfferItem,
    Supplier,
    TaskStatusEnum,
    Tire,
    User,
)
from src.server.services.split_optimizer import OfferCandidate, SplitOptimizerService


def test_split_optimizer_exact_known_optimum_and_savings() -> None:
    """
    Две шины (T1=4 шт., T2=4 шт.) и два поставщика:
    Поставщик 1: T1=5000, T2=8000 (корзина целиком = 52 000)
    Поставщик 2: T1=6000, T2=6500 (корзина целиком = 50 000 -> baseline = 50 000)
    Оптимальный сплит (T1 у Поставщика 1, T2 у Поставщика 2) = 46 000 -> экономия 4 000 (8%).
    """
    offers = [
        OfferCandidate(offer_item_id=1, tire_id=10, sku="T1", price=5000, stock_qty=10, supplier_id=1, supplier_name="S1"),
        OfferCandidate(offer_item_id=2, tire_id=20, sku="T2", price=8000, stock_qty=10, supplier_id=1, supplier_name="S1"),
        OfferCandidate(offer_item_id=3, tire_id=10, sku="T1", price=6000, stock_qty=10, supplier_id=2, supplier_name="S2"),
        OfferCandidate(offer_item_id=4, tire_id=20, sku="T2", price=6500, stock_qty=10, supplier_id=2, supplier_name="S2"),
    ]
    res = SplitOptimizerService.optimize(demands={10: 4, 20: 4}, offers=offers)

    assert res.solver_method == "milp"
    assert res.total_cost == 46000.0
    assert res.baseline_cost == 50000.0
    assert res.savings == 4000.0
    assert res.savings_percent == pytest.approx(8.0)
    assert res.unfulfilled == []


def test_split_optimizer_with_lot_size_quantum() -> None:
    """
    Требуется 6 шт. шины T1:
    Поставщик 1 продает дешевле (5000), но только квантами по 4 шт. (2 кванта = 8 шт. = 40 000).
    Поставщик 2 продает поштучно по 5800.
    Оптимум сплита: 1 квант (4 шт.) у S1 + 2 шт. у S2 = 20 000 + 11 600 = 31 600!
    """
    offers = [
        OfferCandidate(offer_item_id=1, tire_id=1, sku="T1", price=5000, stock_qty=12, lot_size=4, supplier_id=1),
        OfferCandidate(offer_item_id=2, tire_id=1, sku="T1", price=5800, stock_qty=12, lot_size=1, supplier_id=2),
    ]
    res = SplitOptimizerService.optimize(demands={1: 6}, offers=offers)

    assert res.total_cost == 31600.0
    assert res.baseline_cost == 34800.0  # у S2 целиком: 6 * 5800 = 34800 (у S1 было бы 8 * 5000 = 40000)
    assert res.savings == 3200.0
    assert sum(a["qty"] for a in res.allocations) == 6


def test_split_optimizer_stock_shortage_and_greedy_fallback() -> None:
    """При нехватке остатков выкупает максимум доступного и возвращает дефицит в unfulfilled."""
    offers = [
        OfferCandidate(offer_item_id=1, tire_id=1, sku="T1", price=5000, stock_qty=3, lot_size=1, supplier_id=1),
        OfferCandidate(offer_item_id=2, tire_id=1, sku="T1", price=5500, stock_qty=4, lot_size=1, supplier_id=2),
    ]
    res = SplitOptimizerService.optimize(demands={1: 10, 2: 5}, offers=offers)

    assert res.total_cost == 3 * 5000 + 4 * 5500
    assert len(res.unfulfilled) == 2
    by_tid = {u["tire_id"]: u for u in res.unfulfilled}
    assert by_tid[1]["allocated_qty"] == 7
    assert by_tid[1]["shortage_qty"] == 3
    assert by_tid[2]["allocated_qty"] == 0
    assert by_tid[2]["shortage_qty"] == 5

    # Проверка жадного fallback при отказе MILP
    with patch.object(SplitOptimizerService, "_solve_milp", return_value=None):
        greedy_res = SplitOptimizerService.optimize(demands={1: 5}, offers=offers)
    assert greedy_res.solver_method == "greedy"
    assert greedy_res.total_cost == 3 * 5000 + 2 * 5500


async def test_optimize_endpoint_requires_approved_and_persists_result(
    client: AsyncClient,
    db_session: AsyncSession,
    buyer_user: User,
    buyer_token: str,
) -> None:
    tire = Tire(sku="Bridgestone T005 205/55 R16", width_mm=205, profile_ratio=55, diameter_inch=16)
    sup1 = Supplier(name="Поставщик Альфа", inn="7701000001")
    sup2 = Supplier(name="Поставщик Бета", inn="7701000002")
    db_session.add_all([tire, sup1, sup2])
    await db_session.flush()

    sess = ProcurementSession(
        user_id=buyer_user.id,
        title="Сессия оптимизации",
        status=TaskStatusEnum.COMPLETED,
        total_items=2,
        matched_items=2,
    )
    db_session.add(sess)
    await db_session.flush()

    db_session.add_all(
        [
            RawOfferItem(
                session_id=sess.id,
                supplier_id=sup1.id,
                raw_name="Bridgestone 205/55 R16",
                price=7000,
                quantity=4,
                matched_tire_id=tire.id,
                confidence_score=0.95,
                match_status=MatchStatusEnum.AUTO,
            ),
            RawOfferItem(
                session_id=sess.id,
                supplier_id=sup2.id,
                raw_name="Bridgestone Turanza 205/55 R16",
                price=7500,
                quantity=10,
                matched_tire_id=tire.id,
                confidence_score=0.94,
                match_status=MatchStatusEnum.AUTO,
            ),
        ]
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {buyer_token}"}

    # До утверждения (COMPLETED) -> 409
    not_approved = await client.post(
        f"/api/v1/procurement/{sess.id}/optimize",
        headers=headers,
        json={"demand": [{"tire_id": tire.id, "required_qty": 6}]},
    )
    assert not_approved.status_code == 409

    # Утверждаем сессию и запускаем оптимизацию
    await client.post(f"/api/v1/procurement/{sess.id}/approve", headers=headers)
    opt_resp = await client.post(
        f"/api/v1/procurement/{sess.id}/optimize",
        headers=headers,
        json={"demand": [{"tire_id": tire.id, "required_qty": 6}]},
    )
    assert opt_resp.status_code == 200
    body = opt_resp.json()
    assert body["total_cost"] == 4 * 7000 + 2 * 7500  # 43 000
    assert body["baseline_cost"] == 6 * 7500  # 45 000 (только Бета имеет >= 6 шт.)
    assert body["savings"] == 2000.0
