from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any
import xml.etree.ElementTree as ET

import httpx

from src.server.core.config import settings
from src.server.models.procurement import OptimizationResult, ProcurementSession, Supplier


class ERPExporterService:
    """
    Сервис генерации закупочных спецификаций в формате 1С CommerceML 2.08 (XML / JSON)
    и отправки заказов поставщикам во внешнюю ERP-систему по Webhook с HMAC-подписью.
    """

    SCHEMA_VERSION = "2.08"

    @classmethod
    def build_export_data(
        cls,
        session: ProcurementSession,
        opt_result: OptimizationResult,
        suppliers_by_id: dict[int, Supplier] | None = None,
    ) -> dict[str, Any]:
        """Группирует оптимальное распределение по поставщикам в структуру документов 'Заказ поставщику'."""
        suppliers_map = suppliers_by_id or {}
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        grouped: dict[str, dict[str, Any]] = {}
        for idx, alloc in enumerate(opt_result.allocations, start=1):
            sup_id = alloc.get("supplier_id")
            sup_obj = suppliers_map.get(sup_id) if isinstance(sup_id, int) else None

            sup_key = f"supplier_{sup_id}" if sup_id is not None else "supplier_default"
            sup_name = (
                (sup_obj.name if sup_obj else None)
                or alloc.get("supplier_name")
                or (f"Поставщик #{sup_id}" if sup_id is not None else "Основной поставщик")
            )
            sup_inn = sup_obj.inn if sup_obj else None

            if sup_key not in grouped:
                grouped[sup_key] = {
                    "document_id": f"SESSION-{session.id}-{sup_key.upper()}",
                    "document_number": f"{session.id}-{len(grouped) + 1}",
                    "operation": "Заказ поставщику",
                    "currency": "RUB",
                    "supplier": {
                        "id": sup_id,
                        "name": sup_name,
                        "inn": sup_inn,
                    },
                    "items": [],
                    "order_total": 0.0,
                }

            line_total = round(float(alloc["total"]), 2)
            grouped[sup_key]["items"].append(
                {
                    "line_no": idx,
                    "tire_id": alloc["tire_id"],
                    "sku": alloc.get("sku") or f"TIRE-{alloc['tire_id']}",
                    "supplier_article": alloc.get("supplier_article") or "",
                    "qty": int(alloc["qty"]),
                    "lot_size": int(alloc.get("lot_size", 1)),
                    "price": round(float(alloc["price"]), 2),
                    "total": line_total,
                }
            )
            grouped[sup_key]["order_total"] = round(grouped[sup_key]["order_total"] + line_total, 2)

        return {
            "schema_version": f"CommerceML-{cls.SCHEMA_VERSION}",
            "generated_at": now_iso,
            "session_id": session.id,
            "session_title": session.title,
            "status": session.status.value,
            "summary": {
                "solver_method": opt_result.solver_method,
                "total_cost": round(opt_result.total_cost, 2),
                "baseline_cost": round(opt_result.baseline_cost, 2) if opt_result.baseline_cost is not None else None,
                "savings": round(opt_result.savings, 2),
                "savings_percent": round(opt_result.savings_percent, 2),
            },
            "orders": list(grouped.values()),
            "unfulfilled": opt_result.unfulfilled,
        }

    @classmethod
    def generate_commerceml_xml(
        cls,
        session: ProcurementSession,
        opt_result: OptimizationResult,
        suppliers_by_id: dict[int, Supplier] | None = None,
    ) -> bytes:
        """Генерирует валидный XML-файл в стандарте CommerceML 2.08 ('Заказ поставщику')."""
        data = cls.build_export_data(session, opt_result, suppliers_by_id)
        doc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        doc_time = datetime.now(timezone.utc).strftime("%H:%M:%S")

        root = ET.Element(
            "КоммерческаяИнформация",
            attrib={
                "ВерсияСхемы": cls.SCHEMA_VERSION,
                "ДатаФормирования": data["generated_at"],
            },
        )

        for order in data["orders"]:
            doc_el = ET.SubElement(root, "Документ")
            ET.SubElement(doc_el, "Ид").text = str(order["document_id"])
            ET.SubElement(doc_el, "Номер").text = str(order["document_number"])
            ET.SubElement(doc_el, "Дата").text = doc_date
            ET.SubElement(doc_el, "Время").text = doc_time
            ET.SubElement(doc_el, "ХозОперация").text = "Заказ поставщику"
            ET.SubElement(doc_el, "Роль").text = "Покупатель"
            ET.SubElement(doc_el, "Валюта").text = "RUB"
            ET.SubElement(doc_el, "Курс").text = "1"
            ET.SubElement(doc_el, "Сумма").text = f"{order['order_total']:.2f}"
            ET.SubElement(doc_el, "Комментарий").text = f"Сессия закупки #{session.id}: {session.title}"

            # Контрагент (Поставщик)
            contractors_el = ET.SubElement(doc_el, "Контрагенты")
            contractor_el = ET.SubElement(contractors_el, "Контрагент")
            sup = order["supplier"]
            ET.SubElement(contractor_el, "Ид").text = (
                f"SUPPLIER-{sup['id']}" if sup["id"] is not None else "SUPPLIER-DEFAULT"
            )
            ET.SubElement(contractor_el, "Наименование").text = str(sup["name"])
            ET.SubElement(contractor_el, "Роль").text = "Поставщик"
            if sup.get("inn"):
                ET.SubElement(contractor_el, "ИНН").text = str(sup["inn"])

            # Табличная часть товаров
            items_el = ET.SubElement(doc_el, "Товары")
            for item in order["items"]:
                item_el = ET.SubElement(items_el, "Товар")
                ET.SubElement(item_el, "Ид").text = f"TIRE-{item['tire_id']}"
                article = item["supplier_article"] or item["sku"]
                ET.SubElement(item_el, "Артикул").text = str(article)
                ET.SubElement(item_el, "Наименование").text = str(item["sku"])
                unit_el = ET.SubElement(
                    item_el,
                    "БазоваяЕдиница",
                    attrib={"Код": "796", "НаименованиеПолное": "Штука"},
                )
                unit_el.text = "шт"
                ET.SubElement(item_el, "ЦенаЗаЕдиницу").text = f"{item['price']:.2f}"
                ET.SubElement(item_el, "Количество").text = str(item["qty"])
                ET.SubElement(item_el, "Сумма").text = f"{item['total']:.2f}"

        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @classmethod
    def generate_json_bytes(
        cls,
        session: ProcurementSession,
        opt_result: OptimizationResult,
        suppliers_by_id: dict[int, Supplier] | None = None,
    ) -> bytes:
        """Генерирует JSON-спецификацию для интеграции с 1С / ERP."""
        data = cls.build_export_data(session, opt_result, suppliers_by_id)
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def compute_hmac_signature(payload_bytes: bytes, secret: str) -> str:
        """Вычисляет HMAC-SHA256 подпись тела вебхука."""
        digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    @classmethod
    async def send_webhook(
        cls,
        payload: dict[str, Any],
        target_url: str | None = None,
        secret: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Отправляет JSON-спецификацию во внешнюю ERP-систему с заголовком HMAC-подписи."""
        url = (target_url or settings.ERP_WEBHOOK_URL).strip()
        if not url:
            raise ValueError("URL вебхука ERP не настроен (ERP_WEBHOOK_URL пуст).")

        hmac_secret = secret if secret is not None else settings.ERP_WEBHOOK_SECRET
        req_timeout = timeout if timeout is not None else settings.ERP_WEBHOOK_TIMEOUT_SECONDS

        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        signature = cls.compute_hmac_signature(body_bytes, hmac_secret)

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Taires-Signature-256": signature,
            "X-Taires-Session-Id": str(payload.get("session_id", "")),
        }

        async with httpx.AsyncClient(timeout=req_timeout) as client:
            response = await client.post(url, content=body_bytes, headers=headers)
            response.raise_for_status()
            return {
                "status_code": response.status_code,
                "target_url": url,
                "signature": signature,
            }
