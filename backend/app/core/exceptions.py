"""统一业务异常和安全的 HTTP 错误响应。"""

import logging
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """可安全返回给前端的业务异常。"""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = list(details) if details else None


def _request_id(request: Request) -> str:
    """异常发生在中间件之外时也返回合法的请求标识。"""

    return getattr(request.state, "request_id", f"req_{uuid4().hex}")


def _error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Sequence[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """按 API 契约构造统一错误响应。"""

    request_id = _request_id(request)
    content: dict[str, Any] = {
        "code": code,
        "message": message,
        "requestId": request_id,
    }
    if details:
        content["details"] = details

    response_headers = dict(headers or {})
    response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(content),
        headers=response_headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """集中注册业务、校验、HTTP 与未处理异常入口。"""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return _error_response(
            request=request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return _error_response(
            request=request,
            status_code=422,
            code="VALIDATION_ERROR",
            message="请求字段校验失败",
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        message = str(exc.detail) if isinstance(exc.detail, str) else "请求处理失败"
        code = "RESOURCE_NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        return _error_response(
            request=request,
            status_code=exc.status_code,
            code=code,
            message=message,
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        logger.exception(
            "未处理异常",
            exc_info=exc,
            extra={
                "event": "unhandled_exception",
                "request_id": request_id,
                "business_ids": {},
            },
        )
        return _error_response(
            request=request,
            status_code=500,
            code="INTERNAL_SERVER_ERROR",
            message="服务暂时不可用，请稍后重试",
        )
