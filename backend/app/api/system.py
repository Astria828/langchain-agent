"""模型配置、索引状态、日志管理与业务导出接口。"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask as ResponseBackgroundTask

from app.db.database import get_db_session
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import ChromaRepository
from app.schemas.dto import (
    ConnectionTestResult,
    DataResponse,
    DeleteLogsResult,
    IndexStatus,
    LogEntry,
    LogLevel,
    LogRange,
    ModelEndpointPayload,
    ModelGroup,
    ModelSettings,
)
from app.services.system_service import SystemService
from app.tasks.rebuild_index_task import RebuildIndexTask

router = APIRouter(prefix="/api", tags=["system"])
LogLevelFilter = LogLevel | Literal["all"]


def get_model_gateway() -> ModelGateway:
    """创建模型网关，便于测试替换外部 HTTP 调用。"""

    return ModelGateway()


def get_chroma_repository(request: Request) -> ChromaRepository:
    """使用当前应用的数据目录创建索引仓储，保持测试与正式数据隔离。"""

    return ChromaRepository(request.app.state.settings)


def get_system_service(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
) -> SystemService:
    """为单次请求组装事务 Session 与模型网关。"""

    return SystemService(session, gateway, request.app.state.settings)


@router.get("/export/sessions/{session_id}")
def export_session(
    session_id: str,
    service: Annotated[SystemService, Depends(get_system_service)],
) -> Response:
    """下载一个会话的 UTF-8 JSON，文件名使用稳定会话 ID。"""

    content = service.export_session_json(session_id)
    return Response(
        content=content,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Disposition": (f'attachment; filename="loreweave-session-{session_id}.json"'),
        },
    )


@router.get("/export/all")
def export_all(
    service: Annotated[SystemService, Depends(get_system_service)],
) -> FileResponse:
    """下载全量业务 ZIP，并在响应发送完成后回收受管临时文件。"""

    path = service.create_full_export()
    filename = f"loreweave-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        background=ResponseBackgroundTask(service.delete_export_file, path),
    )


@router.get(
    "/logs",
    response_model=DataResponse[list[LogEntry]],
)
def get_logs(
    request: Request,
    service: Annotated[SystemService, Depends(get_system_service)],
    level: Annotated[LogLevelFilter, Query()] = "all",
    range_: Annotated[LogRange, Query(alias="range")] = "all",
) -> DataResponse[list[LogEntry]]:
    """按级别和滚动时间范围返回脱敏日志。"""

    return DataResponse(
        data=service.list_logs(level=level, range_=range_),
        request_id=request.state.request_id,
    )


@router.get("/logs/download")
def download_logs(
    service: Annotated[SystemService, Depends(get_system_service)],
    level: Annotated[LogLevelFilter, Query()] = "all",
    range_: Annotated[LogRange, Query(alias="range")] = "all",
) -> Response:
    """下载与日志页面筛选口径一致的 UTF-8 JSON Lines 文件。"""

    return Response(
        content=service.download_logs(level=level, range_=range_),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition": 'attachment; filename="loreweave-logs.log"',
        },
    )


@router.delete(
    "/logs",
    response_model=DataResponse[DeleteLogsResult],
)
def delete_logs(
    request: Request,
    service: Annotated[SystemService, Depends(get_system_service)],
    level: Annotated[LogLevelFilter, Query()] = "all",
    range_: Annotated[LogRange, Query(alias="range")] = "all",
) -> DataResponse[DeleteLogsResult]:
    """只删除匹配行，并返回实际删除数量。"""

    return DataResponse(
        data=DeleteLogsResult(count=service.delete_logs(level=level, range_=range_)),
        request_id=request.state.request_id,
    )


@router.get(
    "/settings/models",
    response_model=DataResponse[ModelSettings],
)
def get_model_settings(
    request: Request,
    service: Annotated[SystemService, Depends(get_system_service)],
) -> DataResponse[ModelSettings]:
    """读取主模型与 Embedding 的安全公开配置。"""

    return DataResponse(
        data=service.get_model_settings(),
        request_id=request.state.request_id,
    )


@router.post(
    "/settings/models/{group}/test",
    response_model=DataResponse[ConnectionTestResult],
)
async def test_model_connection(
    group: ModelGroup,
    payload: ModelEndpointPayload,
    request: Request,
    service: Annotated[SystemService, Depends(get_system_service)],
) -> DataResponse[ConnectionTestResult]:
    """实际执行最小模型调用并保存测试摘要。"""

    return DataResponse(
        data=await service.test_model(group, payload),
        request_id=request.state.request_id,
    )


@router.put(
    "/settings/models/{group}",
    response_model=DataResponse[ModelSettings],
)
def save_model_settings(
    group: ModelGroup,
    payload: ModelEndpointPayload,
    request: Request,
    service: Annotated[SystemService, Depends(get_system_service)],
) -> DataResponse[ModelSettings]:
    """保存与最近成功测试一致的模型配置。"""

    return DataResponse(
        data=service.save_model(group, payload),
        request_id=request.state.request_id,
    )


@router.get(
    "/index/status",
    response_model=DataResponse[IndexStatus],
)
def get_index_status(
    request: Request,
    service: Annotated[SystemService, Depends(get_system_service)],
) -> DataResponse[IndexStatus]:
    """提供设置页真实联调所需的只读重建状态。"""

    return DataResponse(
        data=service.get_index_status(),
        request_id=request.state.request_id,
    )


@router.post(
    "/index/rebuild",
    response_model=DataResponse[IndexStatus],
)
async def rebuild_index(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    chroma: Annotated[ChromaRepository, Depends(get_chroma_repository)],
) -> DataResponse[IndexStatus]:
    """同步等待当前 Embedding 版本的两个 Collection 全量重建。"""

    task = RebuildIndexTask(session, gateway, chroma)
    await task.run()
    return DataResponse(
        data=IndexStatus(rebuild_required=False),
        request_id=request.state.request_id,
    )
