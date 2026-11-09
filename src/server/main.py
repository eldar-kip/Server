from fastapi import FastAPI
from src.server.api.auth import router as auth_router
from src.server.api.v1.ai_inference import router as ai_inference_router
from src.server.api.v1.ml_ops import router as ml_ops_router
from src.server.api.v1.ml_ops import ws_ml_router
from src.server.api.v1.procurement import router as procurement_router
from src.server.api.v1.websocket import router as ws_router

app = FastAPI(
    title="tAIres Procurement, Trainer & Tire Catalog API",
    description=(
        "Двухконтурная платформа автоматизации закупок шин: контур обучения tAIres-Trainer "
        "с выгрузкой весов и телеметрии в S3, межсервисное сопоставление 'Две таблицы + токен', "
        "Bi-Encoder + pgvector, SplitOptimizer и экспорт в Сайт/CRM и 1С/ERP."
    ),
    version="1.1.0",
)


@app.get("/", tags=["Главная"])
async def root() -> dict[str, str]:
    """Проверка работоспособности сервиса."""
    return {"message": "Добро пожаловать в API каталога шин!"}


app.include_router(auth_router)
app.include_router(procurement_router)
app.include_router(ai_inference_router)
app.include_router(ml_ops_router)
app.include_router(ws_router)
app.include_router(ws_ml_router)
