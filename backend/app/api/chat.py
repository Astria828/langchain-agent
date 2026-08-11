"""真实会话、历史消息与后续流式对话接口。"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as DatabaseSession
from starlette.background import BackgroundTask as ResponseBackgroundTask

from app.api.system import get_chroma_repository, get_model_gateway
from app.db.database import get_db_session
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import ChromaRepository
from app.schemas.dto import (
    CreateSessionPayload,
    DataResponse,
    LongTermMemory,
    MemoryStatus,
    MemoryType,
    Message,
    SendMessagePayload,
    Session,
    UpdateSessionPayload,
)
from app.services.chat_service import ChatService
from app.services.memory_service import MemoryService
from app.tasks.memory_task import run_pending_memory_tasks

router = APIRouter(prefix="/api", tags=["chat"])


def get_chat_service(
    session: Annotated[DatabaseSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    chroma_repository: Annotated[ChromaRepository, Depends(get_chroma_repository)],
) -> ChatService:
    """使用请求级数据库、模型网关和 Chroma 仓储组装会话服务。"""

    return ChatService(session, gateway, chroma_repository)


def get_memory_service(
    session: Annotated[DatabaseSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    chroma_repository: Annotated[ChromaRepository, Depends(get_chroma_repository)],
) -> MemoryService:
    """复用请求级依赖组装长期记忆服务。"""

    return MemoryService(session, gateway, chroma_repository)


@router.get(
    "/sessions",
    response_model=DataResponse[list[Session]],
)
def list_sessions(
    request: Request,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> DataResponse[list[Session]]:
    """按最近更新时间返回全部真实会话。"""

    return DataResponse(data=service.list_sessions(), request_id=request.state.request_id)


@router.post(
    "/sessions",
    response_model=DataResponse[Session],
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    payload: CreateSessionPayload,
    request: Request,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> DataResponse[Session]:
    """创建固定角色、可选世界书和不可变上下文快照。"""

    return DataResponse(
        data=await service.create_session(payload),
        request_id=request.state.request_id,
    )


@router.patch(
    "/sessions/{session_id}",
    response_model=DataResponse[Session],
)
def update_session(
    session_id: str,
    payload: UpdateSessionPayload,
    request: Request,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> DataResponse[Session]:
    """仅修改会话标题。"""

    return DataResponse(
        data=service.update_session(session_id, payload),
        request_id=request.state.request_id,
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_session(
    session_id: str,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> Response:
    """删除会话、上下文快照、消息和快照向量。"""

    service.delete_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sessions/{session_id}/messages",
    response_model=DataResponse[list[Message]],
)
def list_messages(
    session_id: str,
    request: Request,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> DataResponse[list[Message]]:
    """按稳定位置返回会话完整历史消息。"""

    return DataResponse(
        data=service.list_messages(session_id),
        request_id=request.state.request_id,
    )


@router.get(
    "/memories",
    response_model=DataResponse[list[LongTermMemory]],
)
def list_memories(
    request: Request,
    service: Annotated[MemoryService, Depends(get_memory_service)],
    character_id: Annotated[str | None, Query(alias="characterId")] = None,
    memory_type: Annotated[MemoryType | None, Query(alias="type")] = None,
    memory_status: Annotated[MemoryStatus | None, Query(alias="status")] = None,
) -> DataResponse[list[LongTermMemory]]:
    """按角色、类型和状态查询长期记忆。"""

    return DataResponse(
        data=service.list_memories(
            character_id=character_id,
            memory_type=memory_type,
            status=memory_status,
        ),
        request_id=request.state.request_id,
    )


@router.post(
    "/memories/{memory_id}/invalidate",
    response_model=DataResponse[LongTermMemory],
)
def invalidate_memory(
    memory_id: str,
    request: Request,
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> DataResponse[LongTermMemory]:
    """幂等标记长期记忆失效，并在提交后清理向量。"""

    return DataResponse(
        data=service.invalidate_memory(memory_id),
        request_id=request.state.request_id,
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_class=StreamingResponse,
)
async def send_message(
    session_id: str,
    payload: SendMessagePayload,
    request: Request,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> StreamingResponse:
    """保存用户输入，执行双 RAG，并以 SSE 返回角色回复。"""

    messages, config, api_key, retrieved_entries = await service.prepare_chat_turn(
        session_id,
        payload,
    )

    async def event_stream():
        """把服务层业务事件编码为单行 UTF-8 SSE 数据帧。"""

        async for event in service.stream_basic_reply(
            session_id,
            messages,
            config,
            api_key,
            retrieved_entries,
        ):
            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            yield f"data: {data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
        background=ResponseBackgroundTask(
            run_pending_memory_tasks,
            request.app.state.settings,
            service.gateway,
            service.chroma,
        ),
    )
