import asyncio
import hmac
import io
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from src.ml_worker.vector_search import get_configured_model_uri, match_two_tables_in_memory
from src.server.api.deps import authenticate_ws_token
from src.server.core.config import settings
from src.server.core.database import get_db
from src.server.schemas.ml_ops import (
    MatchedNomenclatureRowSchema,
    TwoTablesMatchRequest,
    TwoTablesMatchResponse,
)
from src.server.services.file_parser import FileParserService

router = APIRouter(prefix="/api/v1/ai", tags=["Сервер ИИ (Две таблицы + токен)"])

optional_bearer = HTTPBearer(auto_error=False)


async def verify_ai_token_or_user(
    x_ai_service_token: str | None = Header(
        default=None,
        description="Межсервисный токен доступа из блока 'Две таблицы + токен'",
    ),
    bearer: HTTPAuthorizationCredentials | None = Depends(optional_bearer),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Проверяет межсервисный X-AI-Service-Token либо пользовательский Bearer JWT."""
    if x_ai_service_token and hmac.compare_digest(
        x_ai_service_token.encode("utf-8"),
        settings.AI_SERVICE_TOKEN.encode("utf-8"),
    ):
        return "service_token"

    if bearer and bearer.credentials:
        if hmac.compare_digest(
            bearer.credentials.encode("utf-8"),
            settings.AI_SERVICE_TOKEN.encode("utf-8"),
        ):
            return "service_token"
        user = await authenticate_ws_token(bearer.credentials, db)
        if user is not None:
            return f"user:{user.id}"

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Недействительный токен: передайте заголовок X-AI-Service-Token или Authorization: Bearer",
    )


@router.post("/match-tables", response_model=TwoTablesMatchResponse)
async def match_two_tables_endpoint(
    payload: TwoTablesMatchRequest,
    _caller: str = Depends(verify_ai_token_or_user),
) -> TwoTablesMatchResponse:
    """
    Прямая реализация блока архитектурной схемы 'Две таблицы + токен' ->
    'Возврат сопоставленной номенклатуры' (Предприятие, Поставщик, Цена, Точность совпадения).
    """
    ent_rows = [
        {"id": item.id, "sku": item.sku, "search_text": item.search_text or item.sku}
        for item in payload.enterprise_table
    ]
    sup_rows = [
        {
            "raw_name": item.raw_name,
            "price": item.price,
            "quantity": item.quantity,
            "supplier_article": item.supplier_article,
        }
        for item in payload.supplier_table
    ]

    matched = await asyncio.to_thread(
        match_two_tables_in_memory,
        ent_rows,
        sup_rows,
        payload.top_k,
    )

    return TwoTablesMatchResponse(
        active_model=await asyncio.to_thread(get_configured_model_uri),
        total_rows=len(matched),
        items=[MatchedNomenclatureRowSchema(**row) for row in matched],
    )


def _parse_enterprise_catalog_file(filename: str, content: bytes) -> list[dict[str, object]]:
    lower = filename.lower()
    if not (lower.endswith(".csv") or lower.endswith(".xlsx")):
        raise HTTPException(status_code=400, detail="Таблица предприятия должна быть в формате .csv или .xlsx")
    if lower.endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1251", errors="replace")
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    else:
        df = pd.read_excel(io.BytesIO(content), engine="openpyxl")

    df = df.dropna(how="all")
    if df.empty:
        raise HTTPException(status_code=400, detail="Таблица номенклатуры предприятия пуста")

    # Ищем колонку наименования предприятия или берем первую текстовую колонку
    name_col = None
    id_col = None
    for col in df.columns:
        c_low = str(col).lower().strip()
        if id_col is None and c_low in ("id", "ид", "код"):
            id_col = col
        if name_col is None and any(
            kw in c_low
            for kw in ("предприятие", "номенклатура", "наименование", "название", "товар", "sku", "name")
        ):
            name_col = col
    if name_col is None:
        name_col = df.columns[0]

    rows: list[dict[str, object]] = []
    for idx, r in df.iterrows():
        sku_val = str(r[name_col]).strip() if pd.notna(r[name_col]) else ""
        if not sku_val:
            continue
        ent_id = int(r[id_col]) if id_col is not None and pd.notna(r[id_col]) and str(r[id_col]).isdigit() else int(idx) + 1
        rows.append({"id": ent_id, "sku": sku_val, "search_text": sku_val})
    return rows


@router.post("/match-files", response_model=TwoTablesMatchResponse)
async def match_two_uploaded_files(
    enterprise_file: UploadFile = File(..., description="Таблица номенклатуры предприятия (.xlsx/.csv)"),
    supplier_file: UploadFile = File(..., description="Таблица прайс-листа поставщика (.xlsx/.csv)"),
    top_k: int = Query(5, ge=1, le=50),
    _caller: str = Depends(verify_ai_token_or_user),
) -> TwoTablesMatchResponse:
    """Файловый вариант 'Две таблицы + токен': принимает два файла (.xlsx/.csv) и возвращает сопоставление."""
    ent_bytes = await enterprise_file.read()
    ent_rows = _parse_enterprise_catalog_file(enterprise_file.filename or "enterprise.csv", ent_bytes)

    sup_schemas = await FileParserService.parse_file(supplier_file)
    sup_rows = [
        {"raw_name": s.raw_name, "price": s.price, "quantity": s.quantity}
        for s in sup_schemas
    ]

    matched = await asyncio.to_thread(
        match_two_tables_in_memory,
        ent_rows,
        sup_rows,
        top_k,
    )
    return TwoTablesMatchResponse(
        active_model=await asyncio.to_thread(get_configured_model_uri),
        total_rows=len(matched),
        items=[MatchedNomenclatureRowSchema(**row) for row in matched],
    )
