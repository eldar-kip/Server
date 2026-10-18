from fastapi import FastAPI
from src.server.api.auth import router as auth_router
from src.server.api.v1.procurement import router as procurement_router
app = FastAPI(
    title="Tire Catalog API",
    description="API для управления номенклатурой автомобильных шин",
    version="1.0.0"
)

@app.get("/", tags=["Главная"])
async def root():
    return {"message": "Добро пожаловать в API каталога шин!"}

app.include_router(auth_router)
app.include_router(procurement_router)