import json
from unittest.mock import AsyncMock, MagicMock, patch
import xml.etree.ElementTree as ET
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.server.models import (
    MatchStatusEnum,
    OptimizationResult,
    ProcurementSession,
    RawOfferItem,
    Supplier,
    TaskStatusEnum,
    Tire,
    User,
)
from src.server.services.erp_exporter import ERPExporterService


async def test_commerceml_xml_json_export_and_webhook(
    client: AsyncClient,
    db_session: AsyncSession,
    buyer_user: User,
    buyer_token: str,
) -> None:
    """Проверяет валидность CommerceML XML, JSON-снапшот, скачивание через API и HMAC Webhook."""
    tire = Tire(sku="Michelin Pilot Sport 4 225/45 R17", width_mm=225, profile_ratio=45, diameter_inch=17)
    supplier = Supplier(name="ООО АвтоШина Опт", inn="7701234567")
    db_session.add_all([tire, supplier])
    await db_session.flush()

    session = ProcurementSession(
        user_id=buyer_user.id,
        title="Экспорт 1С",
        status=TaskStatusEnum.APPROVED,
        total_items=1,
        matched_items=1,
    )
    db_session.add(session)
    await db_session.flush()

    offer = RawOfferItem(
        session_id=session.id,
        supplier_id=supplier.id,
        raw_name="Michelin PS4 225/45R17",
        price=11000.0,
        quantity=8,
        supplier_article="MIC-999",
        matched_tire_id=tire.id,
        confidence_score=0.97,
        match_status=MatchStatusEnum.AUTO,
    )
    opt = OptimizationResult(
        session_id=session.id,
        total_cost=44000.0,
        baseline_cost=48000.0,
        savings=4000.0,
        savings_percent=8.33,
        solver_method="milp",
        allocations=[
            {
                "tire_id": tire.id,
                "sku": tire.sku,
                "offer_item_id": 1,
                "supplier_id": supplier.id,
                "supplier_name": supplier.name,
                "supplier_article": "MIC-999",
                "qty": 4,
                "lots": 4,
                "lot_size": 1,
                "price": 11000.0,
                "total": 44000.0,
            }
        ],
        unfulfilled=[],
    )
    db_session.add_all([offer, opt])
    await db_session.commit()

    headers = {"Authorization": f"Bearer {buyer_token}"}

    # 1. Скачивание CommerceML XML
    xml_resp = await client.get(
        f"/api/v1/procurement/{session.id}/export/1c?format=xml",
        headers=headers,
    )
    assert xml_resp.status_code == 200
    assert f'filename="session_{session.id}.xml"' in xml_resp.headers["content-disposition"]

    root = ET.fromstring(xml_resp.content)
    assert root.tag == "КоммерческаяИнформация"
    assert root.attrib["ВерсияСхемы"] == "2.08"
    doc = root.find("Документ")
    assert doc is not None
    assert doc.findtext("ХозОперация") == "Заказ поставщику"
    assert doc.findtext("Сумма") == "44000.00"
    assert doc.findtext("./Контрагенты/Контрагент/ИНН") == "7701234567"
    assert doc.findtext("./Товары/Товар/Артикул") == "MIC-999"
    assert doc.findtext("./Товары/Товар/Количество") == "4"

    # 2. Скачивание JSON-спецификации
    json_resp = await client.get(
        f"/api/v1/procurement/{session.id}/export/1c?format=json",
        headers=headers,
    )
    assert json_resp.status_code == 200
    json_data = json_resp.json()
    assert json_data["schema_version"] == "CommerceML-2.08"
    assert json_data["summary"]["total_cost"] == 44000.0
    assert len(json_data["orders"]) == 1
    assert json_data["orders"][0]["supplier"]["inn"] == "7701234567"

    # 3. Прямая проверка отправки Webhook с HMAC-подписью (мок httpx)
    captured_req: dict[str, object] = {}

    async def _fake_post(url: str, content: bytes, headers: dict[str, str]) -> MagicMock:
        captured_req["url"] = url
        captured_req["content"] = content
        captured_req["headers"] = headers
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        return mock_response

    with patch("src.server.services.erp_exporter.httpx.AsyncClient.post", new=AsyncMock(side_effect=_fake_post)):
        delivery = await ERPExporterService.send_webhook(
            payload=json_data,
            target_url="https://erp.example.com/webhook/1c",
            secret="top-secret",
        )

    assert delivery["status_code"] == 200
    expected_sig = ERPExporterService.compute_hmac_signature(
        json.dumps(json_data, ensure_ascii=False).encode("utf-8"),
        "top-secret",
    )
    sent_headers = captured_req["headers"]
    assert isinstance(sent_headers, dict)
    assert sent_headers["X-Taires-Signature-256"] == expected_sig

    # 4. Эндпоинт POST /export/webhook ставит Celery-таску send_to_erp
    fake_async_result = MagicMock()
    fake_async_result.id = "celery-erp-task-123"
    with patch("src.server.api.v1.procurement.send_to_erp.delay", return_value=fake_async_result) as mock_delay:
        wh_resp = await client.post(
            f"/api/v1/procurement/{session.id}/export/webhook",
            headers=headers,
            json={"webhook_url": "https://erp.example.com/webhook/1c"},
        )
    assert wh_resp.status_code == 200
    assert wh_resp.json()["task_id"] == "celery-erp-task-123"
    assert wh_resp.json()["status"] == "QUEUED"
    mock_delay.assert_called_once_with(session.id, "https://erp.example.com/webhook/1c")
