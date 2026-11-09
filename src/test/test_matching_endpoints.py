from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.server.models import (
    AuditActionEnum,
    FeedbackAuditLog,
    MatchStatusEnum,
    ProcurementSession,
    RawOfferItem,
    TaskStatusEnum,
    Tire,
    User,
)


async def test_matches_filter_manual_rematch_and_approve(
    client: AsyncClient,
    db_session: AsyncSession,
    buyer_user: User,
    buyer_token: str,
) -> None:
    """Тестирует все три эндпоинта TAI-106 и запись в FeedbackAuditLog."""
    tire_auto = Tire(sku="Michelin PS4 225/45 R17", width_mm=225, profile_ratio=45, diameter_inch=17)
    tire_manual = Tire(sku="Pirelli P Zero 225/45 R17", width_mm=225, profile_ratio=45, diameter_inch=17)
    db_session.add_all([tire_auto, tire_manual])
    await db_session.flush()

    session = ProcurementSession(
        user_id=buyer_user.id,
        title="Сессия корректировки",
        status=TaskStatusEnum.COMPLETED,
        total_items=3,
        matched_items=1,
    )
    db_session.add(session)
    await db_session.flush()

    item_high = RawOfferItem(
        session_id=session.id,
        raw_name="Michelin PS4 225/45R17",
        price=10500,
        quantity=4,
        matched_tire_id=tire_auto.id,
        confidence_score=0.93,
        match_status=MatchStatusEnum.AUTO,
        candidates=[{"tire_id": tire_auto.id, "sku": tire_auto.sku, "score": 0.93}],
    )
    item_low = RawOfferItem(
        session_id=session.id,
        raw_name="Pirelli P-Zero 225 45 17",
        price=9900,
        quantity=6,
        matched_tire_id=tire_auto.id,
        confidence_score=0.62,
        match_status=MatchStatusEnum.NEEDS_REVIEW,
        candidates=[
            {"tire_id": tire_auto.id, "sku": tire_auto.sku, "score": 0.62},
            {"tire_id": tire_manual.id, "sku": tire_manual.sku, "score": 0.60},
        ],
    )
    item_unmatched = RawOfferItem(
        session_id=session.id,
        raw_name="Неизвестная шина 195/65 R15",
        price=4500,
        quantity=2,
        matched_tire_id=None,
        confidence_score=0.31,
        match_status=MatchStatusEnum.UNMATCHED,
        candidates=[],
    )
    db_session.add_all([item_high, item_low, item_unmatched])
    await db_session.commit()
    await db_session.refresh(item_low)
    await db_session.refresh(item_unmatched)

    headers = {"Authorization": f"Bearer {buyer_token}"}

    # 1. GET /matches?max_confidence=0.7 возвращает только item_low и item_unmatched
    matches_resp = await client.get(
        f"/api/v1/procurement/{session.id}/matches?max_confidence=0.7",
        headers=headers,
    )
    assert matches_resp.status_code == 200
    matches_data = matches_resp.json()
    assert len(matches_data) == 2
    returned_ids = {m["id"] for m in matches_data}
    assert returned_ids == {item_low.id, item_unmatched.id}

    # 2. POST /approve блокируется с 409, пока есть UNMATCHED без ручной привязки
    approve_conflict = await client.post(
        f"/api/v1/procurement/{session.id}/approve",
        headers=headers,
    )
    assert approve_conflict.status_code == 409

    # 3. PATCH /items/{id} вручную привязывает шины и пишет FeedbackAuditLog
    patch_low = await client.patch(
        f"/api/v1/procurement/items/{item_low.id}",
        headers=headers,
        json={"tire_id": tire_manual.id},
    )
    assert patch_low.status_code == 200
    assert patch_low.json()["match_status"] == "MANUAL"
    assert patch_low.json()["confidence_score"] == 1.0
    assert patch_low.json()["matched_tire"]["id"] == tire_manual.id

    patch_unmatched = await client.patch(
        f"/api/v1/procurement/items/{item_unmatched.id}",
        headers=headers,
        json={"tire_id": tire_manual.id},
    )
    assert patch_unmatched.status_code == 200

    # 4. POST /approve теперь проходит успешно и переводит сессию в APPROVED
    approve_ok = await client.post(
        f"/api/v1/procurement/{session.id}/approve",
        headers=headers,
    )
    assert approve_ok.status_code == 200
    assert approve_ok.json()["status"] == "APPROVED"
    assert approve_ok.json()["matched_items"] == 3

    # 5. Повторное редактирование позиции в APPROVED-сессии запрещено (409)
    patch_after_approve = await client.patch(
        f"/api/v1/procurement/items/{item_low.id}",
        headers=headers,
        json={"tire_id": tire_auto.id},
    )
    assert patch_after_approve.status_code == 409

    # 6. Проверяем записи в FeedbackAuditLog (2 MANUAL_REMATCH + 1 APPROVE)
    logs = list(
        (
            await db_session.execute(
                select(FeedbackAuditLog)
                .where(FeedbackAuditLog.session_id == session.id)
                .order_by(FeedbackAuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(logs) == 3
    assert logs[0].action == AuditActionEnum.MANUAL_REMATCH
    assert logs[0].old_tire_id == tire_auto.id
    assert logs[0].new_tire_id == tire_manual.id
    assert logs[0].old_confidence == 0.62
    assert logs[2].action == AuditActionEnum.APPROVE


async def test_multi_supplier_file_upload_workflow(
    client: AsyncClient,
    buyer_token: str,
) -> None:
    """Проверяет создание поставщиков и загрузку нескольких прайс-листов в одну сессию."""
    from unittest.mock import patch

    headers = {"Authorization": f"Bearer {buyer_token}"}

    s1 = await client.post(
        "/api/v1/procurement/suppliers",
        headers=headers,
        json={"name": "Поставщик 1", "inn": "7701112233"},
    )
    assert s1.status_code == 201
    sup1_id = s1.json()["id"]

    s2 = await client.post(
        "/api/v1/procurement/suppliers",
        headers=headers,
        json={"name": "Поставщик 2", "inn": "7704445566"},
    )
    assert s2.status_code == 201
    sup2_id = s2.json()["id"]

    csv_1 = "Наименование;Цена;Количество\nPirelli P Zero 225/45 R17;10500;4\n".encode("utf-8")
    csv_2 = "Наименование;Цена;Количество\nMichelin PS4 205/55 R16;9800;8\n".encode("utf-8")

    with patch("src.server.api.v1.procurement.process_procurement_session.delay") as mock_delay:
        up1 = await client.post(
            f"/api/v1/procurement/upload?supplier_id={sup1_id}",
            headers=headers,
            files={"file": ("sup1.csv", csv_1, "text/csv")},
        )
        assert up1.status_code == 200
        sess_id = up1.json()["session_id"]
        assert up1.json()["total_items"] == 1

        up2 = await client.post(
            f"/api/v1/procurement/{sess_id}/upload?supplier_id={sup2_id}",
            headers=headers,
            files={"file": ("sup2.csv", csv_2, "text/csv")},
        )
        assert up2.status_code == 200
        assert up2.json()["total_items"] == 2
        assert mock_delay.call_count == 2
