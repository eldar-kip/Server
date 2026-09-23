from fastapi import FastAPI
from src.server.core.database import engine, Base
from src.server.models.user import User

Base.metadata.create_all(bind=engine)
app = FastAPI()


@app.get("/")
async def root():
    return {"message": "Hello World"}