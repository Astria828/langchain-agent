"""用户身份、角色卡与世界书管理接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.api.system import get_chroma_repository, get_model_gateway
from app.db.database import get_db_session
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import ChromaRepository
from app.schemas.dto import (
    Character,
    ConfirmWorldBookDraftsPayload,
    DataResponse,
    ReembedResult,
    UpdateCharacterPayload,
    UpdateUserIdentityPayload,
    UpdateWorldBookEntryPayload,
    UpdateWorldBookPayload,
    UserIdentity,
    WorldBook,
    WorldBookDraftEntry,
    WorldBookEntry,
)
from app.services.content_service import ContentService
from app.services.worldbook_service import WorldBookService

router = APIRouter(prefix="/api", tags=["content"])


def get_content_service(
    session: Annotated[Session, Depends(get_db_session)],
) -> ContentService:
    """使用当前请求的数据库 Session 创建内容服务。"""

    return ContentService(session)


def get_worldbook_service(
    session: Annotated[Session, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    chroma_repository: Annotated[ChromaRepository, Depends(get_chroma_repository)],
) -> WorldBookService:
    """复用请求级数据库、模型网关与 Chroma 仓储组装世界书服务。"""

    return WorldBookService(session, gateway, chroma_repository)


@router.get(
    "/identity",
    response_model=DataResponse[UserIdentity],
)
def get_identity(
    request: Request,
    service: Annotated[ContentService, Depends(get_content_service)],
) -> DataResponse[UserIdentity]:
    """返回当前单用户身份。"""

    return DataResponse(
        data=service.get_identity(),
        request_id=request.state.request_id,
    )


@router.put(
    "/identity",
    response_model=DataResponse[UserIdentity],
)
def update_identity(
    payload: UpdateUserIdentityPayload,
    request: Request,
    service: Annotated[ContentService, Depends(get_content_service)],
) -> DataResponse[UserIdentity]:
    """部分更新当前身份。"""

    return DataResponse(
        data=service.update_identity(payload),
        request_id=request.state.request_id,
    )


@router.get(
    "/characters",
    response_model=DataResponse[list[Character]],
)
def list_characters(
    request: Request,
    service: Annotated[ContentService, Depends(get_content_service)],
) -> DataResponse[list[Character]]:
    """返回全部角色卡。"""

    return DataResponse(
        data=service.list_characters(),
        request_id=request.state.request_id,
    )


@router.post(
    "/characters",
    response_model=DataResponse[Character],
    status_code=status.HTTP_201_CREATED,
)
def create_character(
    request: Request,
    service: Annotated[ContentService, Depends(get_content_service)],
) -> DataResponse[Character]:
    """使用契约默认值创建角色卡。"""

    return DataResponse(
        data=service.create_character(),
        request_id=request.state.request_id,
    )


@router.put(
    "/characters/{character_id}",
    response_model=DataResponse[Character],
)
def update_character(
    character_id: str,
    payload: UpdateCharacterPayload,
    request: Request,
    service: Annotated[ContentService, Depends(get_content_service)],
) -> DataResponse[Character]:
    """部分更新指定角色卡。"""

    return DataResponse(
        data=service.update_character(character_id, payload),
        request_id=request.state.request_id,
    )


@router.delete(
    "/characters/{character_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_character(
    character_id: str,
    service: Annotated[ContentService, Depends(get_content_service)],
) -> Response:
    """删除未被历史会话引用的角色卡。"""

    service.delete_character(character_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/worldbooks",
    response_model=DataResponse[list[WorldBook]],
)
def list_world_books(
    request: Request,
    service: Annotated[WorldBookService, Depends(get_worldbook_service)],
) -> DataResponse[list[WorldBook]]:
    """返回全部世界书及其正式条目。"""

    return DataResponse(data=service.list_world_books(), request_id=request.state.request_id)


@router.post(
    "/worldbooks",
    response_model=DataResponse[WorldBook],
    status_code=status.HTTP_201_CREATED,
)
def create_world_book(
    request: Request,
    service: Annotated[WorldBookService, Depends(get_worldbook_service)],
) -> DataResponse[WorldBook]:
    """创建空世界书。"""

    return DataResponse(data=service.create_world_book(), request_id=request.state.request_id)


@router.put(
    "/worldbooks/{world_book_id}",
    response_model=DataResponse[WorldBook],
)
def update_world_book(
    world_book_id: str,
    payload: UpdateWorldBookPayload,
    request: Request,
    service: Annotated[WorldBookService, Depends(get_worldbook_service)],
) -> DataResponse[WorldBook]:
    """部分更新世界书名称或完整原文。"""

    return DataResponse(
        data=service.update_world_book(world_book_id, payload),
        request_id=request.state.request_id,
    )


@router.delete(
    "/worldbooks/{world_book_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_world_book(
    world_book_id: str,
    service: Annotated[WorldBookService, Depends(get_worldbook_service)],
) -> Response:
    """删除未被历史会话引用的世界书并清理向量。"""

    service.delete_world_book(world_book_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/worldbooks/{world_book_id}/split",
    response_model=DataResponse[list[WorldBookDraftEntry]],
)
async def split_world_book(
    world_book_id: str,
    request: Request,
    service: Annotated[WorldBookService, Depends(get_worldbook_service)],
) -> DataResponse[list[WorldBookDraftEntry]]:
    """使用主模型将完整原文整理为未入库草稿。"""

    return DataResponse(
        data=await service.split_world_book(world_book_id),
        request_id=request.state.request_id,
    )


@router.post(
    "/worldbooks/{world_book_id}/entries/confirm",
    response_model=DataResponse[list[WorldBookEntry]],
    status_code=status.HTTP_201_CREATED,
)
async def confirm_world_book_drafts(
    world_book_id: str,
    payload: ConfirmWorldBookDraftsPayload,
    request: Request,
    service: Annotated[WorldBookService, Depends(get_worldbook_service)],
) -> DataResponse[list[WorldBookEntry]]:
    """确认草稿为正式条目并尝试生成向量。"""

    return DataResponse(
        data=await service.confirm_drafts(world_book_id, payload),
        request_id=request.state.request_id,
    )


@router.post(
    "/worldbooks/{world_book_id}/entries",
    response_model=DataResponse[WorldBookEntry],
    status_code=status.HTTP_201_CREATED,
)
def create_world_book_entry(
    world_book_id: str,
    request: Request,
    service: Annotated[WorldBookService, Depends(get_worldbook_service)],
) -> DataResponse[WorldBookEntry]:
    """在世界书末尾创建一个待编辑、待索引条目。"""

    return DataResponse(
        data=service.create_entry(world_book_id),
        request_id=request.state.request_id,
    )


@router.put(
    "/worldbooks/{world_book_id}/entries/{entry_id}",
    response_model=DataResponse[WorldBookEntry],
)
def update_world_book_entry(
    world_book_id: str,
    entry_id: str,
    payload: UpdateWorldBookEntryPayload,
    request: Request,
    service: Annotated[WorldBookService, Depends(get_worldbook_service)],
) -> DataResponse[WorldBookEntry]:
    """部分更新正式条目的六个可编辑字段。"""

    return DataResponse(
        data=service.update_entry(world_book_id, entry_id, payload),
        request_id=request.state.request_id,
    )


@router.delete(
    "/worldbooks/{world_book_id}/entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_world_book_entry(
    world_book_id: str,
    entry_id: str,
    service: Annotated[WorldBookService, Depends(get_worldbook_service)],
) -> Response:
    """删除正式条目并清理对应向量。"""

    service.delete_entry(world_book_id, entry_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/worldbooks/{world_book_id}/reembed",
    response_model=DataResponse[ReembedResult],
)
async def reembed_world_book(
    world_book_id: str,
    request: Request,
    service: Annotated[WorldBookService, Depends(get_worldbook_service)],
) -> DataResponse[ReembedResult]:
    """重新生成当前世界书中待处理或版本过期的向量。"""

    return DataResponse(
        data=await service.reembed(world_book_id),
        request_id=request.state.request_id,
    )
