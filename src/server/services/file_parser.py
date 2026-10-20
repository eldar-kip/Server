import io
import pandas as pd
from fastapi import UploadFile, HTTPException
from src.server.schemas.procurement import RawOfferItemSchema

class FileParserService:
    # Ключевые слова для автоопределения колонок
    COLUMN_MAPPING = {
        "raw_name": ["наименование", "название", "товар", "номенклатура", "описание", "raw_name", "name", "title"],
        "price": ["цена", "стоимость", "прайс", "price", "cost"],
        "quantity": ["количество", "кол-во", "остаток", "шт", "quantity", "qty", "count"],
        "supplier_article": ["артикул", "код", "sku", "article", "supplier_article"]
    }

    @staticmethod
    async def parse_file(file: UploadFile) -> list[RawOfferItemSchema]:
        # Валидация формата файла (.xlsx, .csv)[cite: 7]
        filename = file.filename.lower() if file.filename else ""
        if not (filename.endswith(".xlsx") or filename.endswith(".csv")):
            raise HTTPException(status_code=400, detail="Разрешены только файлы .xlsx и .csv")

        content = await file.read()
        
        try:
            # Чтение файлов через pandas / openpyxl
            if filename.endswith(".csv"):
                try:
                    text = content.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = content.decode("cp1251", errors="replace")
                df = pd.read_csv(io.StringIO(text), sep=None, engine="python")
            else:
                df = pd.read_excel(io.BytesIO(content), engine="openpyxl")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Ошибка чтения файла: {e}")

        # Обработка ошибок: удаление полностью пустых строк
        df = df.dropna(how='all')
        
        # Автоопределение колонок по ключевым словам
        columns_map = FileParserService._detect_columns(df.columns)
        
        if "raw_name" not in columns_map.values() or "price" not in columns_map.values():
            raise HTTPException(
                status_code=400, 
                detail="Не найдены обязательные колонки (Наименование, Цена)."
            )

        df = df.rename(columns=columns_map)
        
        items = []
        for _, row in df.iterrows():
            try:
                raw_name = str(row["raw_name"]).strip() if pd.notna(row.get("raw_name")) else ""
                price = float(row["price"]) if pd.notna(row.get("price")) else 0.0
                quantity = int(row.get("quantity", 1)) if pd.notna(row.get("quantity")) else 1
                supplier_article = str(row["supplier_article"]).strip() if "supplier_article" in row and pd.notna(row["supplier_article"]) else None

                # Обработка ошибок: пропуск отсутствующих или нулевых цен[cite: 4]
                if not raw_name or price <= 0:
                    continue

                # Валидация по схеме[cite: 4, 5]
                item = RawOfferItemSchema(
                    raw_name=raw_name,
                    price=price,
                    quantity=max(quantity, 1),
                    supplier_article=supplier_article
                )
                items.append(item) # type: ignore
            except (ValueError, TypeError):
                # Пропускаем строки с невалидными форматами (например, текст в колонке цены)
                continue

        if not items:
            raise HTTPException(status_code=400, detail="Файл не содержит валидных позиций (проверьте наличие цен > 0).")

        return items # type: ignore

    @staticmethod
    def _detect_columns(headers: pd.Index) -> dict[str, str]:
        mapped_columns = {}
        headers_lower = {str(col).lower().strip(): col for col in headers}

        for target_col, keywords in FileParserService.COLUMN_MAPPING.items():
            for header_lower, original_header in headers_lower.items():
                if any(keyword in header_lower for keyword in keywords):
                    if original_header not in mapped_columns:
                        mapped_columns[original_header] = target_col
                        break
        return mapped_columns # type: ignore
