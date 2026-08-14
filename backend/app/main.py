"""FastAPI 应用入口、中间件和健康检查。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from time import perf_counter
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.api.chat import router as chat_router
from app.api.content import router as content_router
from app.api.system import router as system_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import bind_request_id, configure_logging, reset_request_id
from app.db.database import create_database_engine, create_session_factory
from app.schemas.dto import DataResponse
from app.tasks.memory_task import (
    recover_interrupted_memory_tasks,
    run_pending_memory_tasks,
)
from app.tasks.rebuild_index_task import recover_interrupted_index_rebuilds
from app.tasks.vector_cleanup_task import (
    recover_interrupted_vector_cleanup_tasks,
    run_pending_vector_cleanup_tasks,
)

logger = logging.getLogger(__name__)


class HealthData(BaseModel):
    """健康检查的业务数据。"""

    status: Literal["ok"] = "ok"


class HealthResponse(DataResponse[HealthData]):
    """符合统一响应壳的健康检查响应。"""


def create_app(app_settings: Settings | None = None) -> FastAPI:
    """按给定配置创建应用，便于运行环境与测试环境相互隔离。"""

    settings = app_settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        """启动时恢复中断任务；数据库尚未初始化时仍允许健康检查启动。"""

        background_runner: asyncio.Task[None] | None = None
        if settings.database_path.exists():
            engine = create_database_engine(settings)
            try:
                with create_session_factory(engine)() as session:
                    recover_interrupted_index_rebuilds(session)
                    recover_interrupted_memory_tasks(session)
                    recover_interrupted_vector_cleanup_tasks(session)
            finally:
                engine.dispose()

            async def run_recoverable_tasks() -> None:
                """依次处理记忆整理及其可能产生的残留向量清理。"""

                await run_pending_memory_tasks(settings)
                await run_pending_vector_cleanup_tasks(settings)

            background_runner = asyncio.create_task(run_recoverable_tasks())
        try:
            yield
        finally:
            if background_runner is not None:
                if not background_runner.done():
                    background_runner.cancel()
                with suppress(asyncio.CancelledError):
                    await background_runner

    application = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="织境单用户角色扮演 Agent 的本地 API。",
        lifespan=lifespan,
    )
    application.state.settings = settings

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @application.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """为请求绑定链路 ID，并记录不含正文和查询参数的完成事件。"""

        request_id = f"req_{uuid4().hex}"
        request.state.request_id = request_id
        token = bind_request_id(request_id)
        started_at = perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "HTTP 请求处理完成",
                extra={
                    "event": "http_request_completed",
                    "business_ids": {
                        "method": request.method,
                        "path": request.url.path,
                        "statusCode": response.status_code,
                        "durationMs": round((perf_counter() - started_at) * 1000),
                    },
                },
            )
            return response
        except Exception:
            logger.exception(
                "HTTP 请求处理失败",
                extra={
                    "event": "http_request_failed",
                    "business_ids": {
                        "method": request.method,
                        "path": request.url.path,
                    },
                },
            )
            raise
        finally:
            reset_request_id(token)

    register_exception_handlers(application)
    application.include_router(chat_router)
    application.include_router(content_router)
    application.include_router(system_router)

    @application.get(
        "/api/health",
        tags=["system"],
        response_model=HealthResponse,
    )
    async def health(request: Request) -> HealthResponse:
        """只检查应用进程，不访问数据库或外部模型。"""

        return HealthResponse(
            data=HealthData(),
            request_id=request.state.request_id,
        )

    return application


app = create_app()
