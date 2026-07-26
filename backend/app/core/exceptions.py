from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        details: dict | None = None,
        status_code: int = 400,
        *,
        stage: str = "VALIDATION",
        retryable: bool | None = None,
        diagnostic_id: str | None = None,
        tool_execution_completed: bool = False,
    ):
        self.code, self.message, self.details, self.status_code = (
            code,
            message,
            details or {},
            status_code,
        )
        self.stage = stage
        self.retryable = status_code >= 500 if retryable is None else retryable
        self.diagnostic_id = diagnostic_id or str(uuid4())
        self.tool_execution_completed = tool_execution_completed

    def envelope(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "retryable": self.retryable,
            "diagnostic_id": self.diagnostic_id,
            "tool_execution_completed": self.tool_execution_completed,
            "details": self.details,
        }


async def domain_error_handler(_: Request, error: DomainError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.envelope())
