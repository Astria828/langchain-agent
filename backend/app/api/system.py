"""模型配置与只读索引状态接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db.database import get_db_session
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import ChromaRepository
from app.schemas.dto import (
    ConnectionTestResult,
    DataResponse,
    IndexStatus,
    ModelEndpointPayload,
    ModelGroup,
    ModelSettings,
)
from app.services.system_service import SystemService
from app.tasks.rebuild_index_task import RebuildIndexTask

router = APIRouter(prefix="/api", tags=["system"])


def get_model_gateway() -> ModelGateway:
    """创建模型网关，便于测试替换外部 HTTP 调用。"""

    return ModelGateway()


def get_chroma_repository(request: Request) -> ChromaRepository:
    """使用当前应用的数据目录创建索引仓储，保持测试与正式数据隔离。"""

    return ChromaRepository(request.app.state.settings)


def get_system_service(
    session: Annotated[Session, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
) -> SystemService:
    """为单次请求组装事务 Session 与模型网关。"""

    return SystemService(session, gateway)


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
