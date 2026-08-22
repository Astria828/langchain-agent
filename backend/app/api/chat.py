"""真实会话、历史消息与后续流式对话接口。"""

import json
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as DatabaseSession

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
    RecommendedReplyOutput,
    SendMessagePayload,
    Session,
    UpdateSessionPayload,
)
from app.services.chat_service import ChatService
from app.services.memory_service import MemoryService
from app.tasks.memory_task import run_pending_memory_tasks
from app.tasks.vector_cleanup_task import run_pending_vector_cleanup_tasks

router = APIRouter(prefix="/api", tags=["chat"])

# SSE 注释帧：先推一帧把响应头冲出去，让前端 fetch() 立刻拿到可读的 body，
# 后续的检索与生成才不会把整段等待压在连接建立之前。解析端按规范忽略它。
SSE_READY_FRAME = ": ready\n\n"


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
    request: Request,
    background_tasks: BackgroundTasks,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> Response:
    """删除会话、上下文快照、消息和快照向量。"""

    service.delete_session(session_id)
    background_tasks.add_task(
        run_pending_vector_cleanup_tasks,
        request.app.state.settings,
        service.chroma,
    )
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


@router.delete(
    "/sessions/{session_id}/turns/{assistant_message_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_turn(
    session_id: str,
    assistant_message_id: str,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> Response:
    """物理删除最后一轮用户消息和助手回复。"""

    service.delete_turn(session_id, assistant_message_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sessions/{session_id}/recommended-reply",
    response_model=DataResponse[RecommendedReplyOutput],
)
async def recommended_reply(
    session_id: str,
    request: Request,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> DataResponse[RecommendedReplyOutput]:
    """生成一条不落库、不自动发送的用户侧推荐回复。"""

    return DataResponse(
        data=await service.generate_recommended_reply(session_id),
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
    background_tasks: BackgroundTasks,
    service: Annotated[MemoryService, Depends(get_memory_service)],
) -> DataResponse[LongTermMemory]:
    """幂等标记长期记忆失效，并在提交后清理向量。"""

    response = DataResponse(
        data=service.invalidate_memory(memory_id),
        request_id=request.state.request_id,
    )
    background_tasks.add_task(
        run_pending_vector_cleanup_tasks,
        request.app.state.settings,
        service.chroma,
    )
    return response


@router.post(
    "/sessions/{session_id}/messages",
    response_class=StreamingResponse,
)
async def send_message(
    session_id: str,
    payload: SendMessagePayload,
    request: Request,
    background_tasks: BackgroundTasks,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> StreamingResponse:
    """保存用户输入，随即建立 SSE；双 RAG 与生成都在流内进行。"""

    start = service.begin_chat_turn(session_id, payload)

    async def event_stream():
        """把服务层业务事件编码为单行 UTF-8 SSE 数据帧。"""

        yield SSE_READY_FRAME
        async for event in service.stream_basic_reply(session_id, start):
            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            yield f"data: {data}\n\n"

    background_tasks.add_task(
        run_pending_memory_tasks,
        request.app.state.settings,
        service.gateway,
        service.chroma,
    )
    background_tasks.add_task(
        run_pending_vector_cleanup_tasks,
        request.app.state.settings,
        service.chroma,
    )
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
        background=background_tasks,
    )


async def _message_action_response(
    *,
    session_id: str,
    assistant_message_id: str,
    action: str,
    service: ChatService,
) -> StreamingResponse:
    """校验后立即建立 SSE，双 RAG 与生成都在流内进行。"""

    start = service.begin_message_action(session_id, assistant_message_id, action)

    async def event_stream():
        """把消息操作事件编码为 UTF-8 SSE 数据帧。"""

        yield SSE_READY_FRAME
        async for event in service.stream_message_action(session_id, action, start):
            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            yield f"data: {data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@router.post(
    "/sessions/{session_id}/messages/{assistant_message_id}/regenerate",
    response_class=StreamingResponse,
)
async def regenerate_message(
    session_id: str,
    assistant_message_id: str,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> StreamingResponse:
    """重新生成并原位覆盖最后一条助手回复。"""

    return await _message_action_response(
        session_id=session_id,
        assistant_message_id=assistant_message_id,
        action="regenerate",
        service=service,
    )


@router.post(
    "/sessions/{session_id}/messages/{assistant_message_id}/continue",
    response_class=StreamingResponse,
)
async def continue_message(
    session_id: str,
    assistant_message_id: str,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> StreamingResponse:
    """继续生成并追加到最后一条助手回复。"""

    return await _message_action_response(
        session_id=session_id,
        assistant_message_id=assistant_message_id,
        action="continue",
        service=service,
    )
