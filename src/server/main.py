from fastapi import FastAPI

app = FastAPI(
    title="Tire Catalog API",
    description="API для управления номенклатурой автомобильных шин",
    version="1.0.0"
)

@app.get("/", tags=["Главная"])
async def root():
    return {"message": "Добро пожаловать в API каталога шин!"}