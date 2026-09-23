from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Указываем путь к базе данных. 
# Используем папку db, которую ты создал в корне проекта.
SQLALCHEMY_DATABASE_URL = "sqlite:///./db/app.db"

# Создаем движок
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

# Создаем фабрику сессий
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Базовый класс для моделей
Base = declarative_base()