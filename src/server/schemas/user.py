from pydantic import BaseModel, ConfigDict, EmailStr

# Схема для получения данных при регистрации и логине
class UserCreate(BaseModel):
    email: EmailStr
    password: str

# Схема для ответа (чтобы отдавать ORM-объект User)
class UserResponse(BaseModel):
    id: int
    email: EmailStr
    role: str

    model_config = ConfigDict(from_attributes=True)  #  ОБЯЗАТЕЛЬНО для SQLAlchemy

# Схема для возврата токена
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"