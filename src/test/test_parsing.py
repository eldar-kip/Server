import pytest
import io
from fastapi import UploadFile, HTTPException
from src.server.services.file_parser import FileParserService

@pytest.mark.asyncio
async def test_parse_csv_success():
    csv_data = "Наименование,Цена,Количество,Артикул\nШина Pirelli, 5000, 4, PIR123\nШина Michelin, 6000, 2, MIC456"
    file = UploadFile(filename="test.csv", file=io.BytesIO(csv_data.encode("utf-8")))
    
    items = await FileParserService.parse_file(file)
    assert len(items) == 2
    assert items[0].raw_name == "Шина Pirelli"
    assert items[0].price == 5000.0
    assert items[0].supplier_article == "PIR123"

@pytest.mark.asyncio
async def test_missing_required_columns():
    csv_data = "Количество,Артикул\n4, PIR123"
    file = UploadFile(filename="test.csv", file=io.BytesIO(csv_data.encode("utf-8")))
    
    with pytest.raises(HTTPException) as exc:
         await FileParserService.parse_file(file)
    assert exc.value.status_code == 400
    assert "Не найдены обязательные колонки" in exc.value.detail

@pytest.mark.asyncio
async def test_skip_invalid_prices_and_empty_rows():
    csv_data = "Наименование,Цена\nШина Pirelli, 5000\nПустая цена,\nМинус,-100\nТолько текст,Бесплатно"
    file = UploadFile(filename="test.csv", file=io.BytesIO(csv_data.encode("utf-8")))
    
    items = await FileParserService.parse_file(file)
    # Из 4 строк только первая имеет валидную цену > 0
    assert len(items) == 1
    assert items[0].raw_name == "Шина Pirelli"