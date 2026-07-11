from fastapi import Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    def __init__(
        self, code: str, message: str, details: dict | None = None, status_code: int = 400
    ):
        self.code, self.message, self.details, self.status_code = (
            code,
            message,
            details or {},
            status_code,
        )


async def domain_error_handler(_: Request, error: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"code": error.code, "message": error.message, "details": error.details},
    )
