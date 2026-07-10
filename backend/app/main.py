from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import router
from app.core.config import get_settings
from app.core.exceptions import DomainError, domain_error_handler

app = FastAPI(title="CTF Web Agent API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origin_list, allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
app.add_exception_handler(DomainError, domain_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"code": "VALIDATION_ERROR", "message": "Request validation failed.", "details": {"errors": error.errors()}})


app.include_router(router)
