import io
import pandas as pd
import pytest
from fastapi import HTTPException, UploadFile
from src.server.services.file_parser import FileParserService


async def test_parse_csv_success() -> None:
    csv_data = (
        "Наименование,Цена,Количество,Артикул,Квант,Мин партия\n"
        "Шина Pirelli, 5000, 8, PIR123, 4, 4\n"
        "Шина Michelin, 6000, 2, MIC456, 1, 2"
    )
    file = UploadFile(filename="test.csv", file=io.BytesIO(csv_data.encode("utf-8")))

    items = await FileParserService.parse_file(file)
    assert len(items) == 2
    assert items[0].raw_name == "Шина Pirelli"
    assert items[0].price == 5000.0
    assert items[0].quantity == 8
    assert items[0].lot_size == 4
    assert items[0].min_order_qty == 4
    assert items[0].supplier_article == "PIR123"


async def test_parse_xlsx_success() -> None:
    buf = io.BytesIO()
    df = pd.DataFrame(
        [
            {"Товар": "Nokian Hakkapeliitta 10 205/55 R16", "Стоимость": 11200, "Остаток": 12},
        ]
    )
    df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)

    file = UploadFile(filename="price.xlsx", file=buf)
    items = await FileParserService.parse_file(file)
    assert len(items) == 1
    assert items[0].price == 11200.0
    assert items[0].quantity == 12
    assert items[0].lot_size == 1


async def test_missing_required_columns() -> None:
    csv_data = "Количество,Артикул\n4, PIR123"
    file = UploadFile(filename="test.csv", file=io.BytesIO(csv_data.encode("utf-8")))

    with pytest.raises(HTTPException) as exc:
        await FileParserService.parse_file(file)
    assert exc.value.status_code == 400
    assert "Не найдены обязательные колонки" in exc.value.detail


async def test_skip_invalid_prices_and_empty_rows() -> None:
    csv_data = (
        "Наименование,Цена\n"
        "Шина Pirelli, 5000\n"
        "Пустая цена,\n"
        "Минус,-100\n"
        "Только текст,Бесплатно"
    )
    file = UploadFile(filename="test.csv", file=io.BytesIO(csv_data.encode("utf-8")))

    items = await FileParserService.parse_file(file)
    assert len(items) == 1
    assert items[0].raw_name == "Шина Pirelli"
