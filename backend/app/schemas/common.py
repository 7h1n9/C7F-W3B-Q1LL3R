from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    data: T


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict
