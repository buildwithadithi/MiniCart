from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


_MAX_INT64 = 9223372036854775807
_MAX_INT32 = 2147483647


class OrderItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0, le=_MAX_INT64, strict=True)
    quantity: int = Field(gt=0, le=_MAX_INT32, strict=True)


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[OrderItemInput] = Field(min_length=1)


class OrderItemResponse(BaseModel):
    id: int
    product_id: int
    quantity: int
    unit_price: str


class OrderResponse(BaseModel):
    id: int
    total_amount: str
    status: str
    created_at: datetime
    items: list[OrderItemResponse]
    payment_id: int
    payment_status: str


class HealthResponse(BaseModel):
    status: str


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Name must not be blank.")
        return normalized


class RegisterResponse(BaseModel):
    id: int
    name: str
    email: EmailStr
    created_at: datetime


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
