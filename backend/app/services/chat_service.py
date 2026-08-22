"""真实会话、不可变上下文快照与历史消息管理。"""

import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from threading import RLock
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DatabaseSession

from app.chains.chat_chain import (
    build_recommended_reply_chain,
    stream_basic_chat_blocks,
)
from app.core.exceptions import AppError
from app.core.security import unprotect_api_key
from app.db.models import (
    BackgroundTask,
    Character,
    ChatSession,
    LongTermMemory,
    ModelConfig,
    UserIdentity,
    WorldBook,
    WorldBookEntry,
    utc_now,
)
from app.db.models import Message as MessageModel
from app.db.models import MessageBlock as MessageBlockModel
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import ChromaRepository, calculate_content_hash
from app.repositories.sqlite_repository import SQLiteRepository
from app.retrievers.memory_retriever import MemoryRetriever
from app.retrievers.worldbook_retriever import WorldBookRetriever
from app.schemas.dto import (
    ChatReplyBlock,
    ChatReplyOutput,
    CreateSessionPayload,
    Message,
    MessageBlock,
    RecommendedReplyOutput,
    SendMessagePayload,
    Session,
    UpdateSessionPayload,
)

logger = logging.getLogger(__name__)
RETRIEVAL_HISTORY_ROUNDS = 4
# 角色卡被删时会话仍应可读；绑定被 RESTRICT 外键保护，正常路径到不了这里。
DELETED_CHARACTER_NAME = "已删除角色卡"
# 重说与继续说的检索文本和上一次逐字相同，缓存住就不必重算同一个向量。
# 单条 4096 维向量约 130KB，容量取小值即可覆盖“连点几次重说”的场景。
RETRIEVAL_EMBEDDING_CACHE_SIZE = 8

_retrieval_embedding_lock = RLock()
_retrieval_embeddings: OrderedDict[tuple[int, str, str], list[float]] = OrderedDict()


def _cached_retrieval_embedding(key: tuple[int, str, str]) -> list[float] | None:
    """取出缓存的检索向量并刷新其最近使用顺序。"""

    with _retrieval_embedding_lock:
        embedding = _retrieval_embeddings.get(key)
        if embedding is not None:
            _retrieval_embeddings.move_to_end(key)
        return embedding


def _store_retrieval_embedding(key: tuple[int, str, str], embedding: list[float]) -> None:
    """只缓存成功结果，避免一次瞬时失败影响后续轮次。"""

    with _retrieval_embedding_lock:
        _retrieval_embeddings[key] = embedding
        _retrieval_embeddings.move_to_end(key)
        while len(_retrieval_embeddings) > RETRIEVAL_EMBEDDING_CACHE_SIZE:
            _retrieval_embeddings.popitem(last=False)
CONTINUE_REPLY_INSTRUCTION = (
    "继续上一条角色回复，从结束处自然续写。"
    "不要重复已有内容，也不要假设用户产生了新的动作或台词。"
)


def _is_unanswered(message: MessageModel) -> bool:
    """判断消息是否为已发出却没能配对回复的断层消息。

    只有用户消息会出现这种状态：助手回复要么带轮次编号完整落库，要么根本不落库。
    开场白是没有轮次编号的助手消息，属于会话正常内容，不算断层。
    """

    return message.role == "user" and message.turn_number is None


def session_to_dto(
    repository: SQLiteRepository,
    chat_session: ChatSession,
    identity: UserIdentity,
) -> Session:
    """按当前身份、角色卡与世界书绑定组装会话公开 DTO。

    会话 API 与系统导出共用这一份解析规则，避免两处各自拼字段后逐渐漂移。
    """

    character = repository.get(Character, chat_session.character_id)
    world_book = (
        repository.get(WorldBook, chat_session.world_book_id)
        if chat_session.world_book_id is not None
        else None
    )
    return Session(
        id=chat_session.id,
        title=chat_session.title,
        character_id=chat_session.character_id,
        world_book_id=chat_session.world_book_id,
        identity_name=identity.name,
        identity_persona_name=identity.persona_name,
        character_name=character.name if character is not None else DELETED_CHARACTER_NAME,
        world_book_name=world_book.name if world_book is not None else None,
        round_count=chat_session.round_count,
        consolidated_round=chat_session.consolidated_round,
        summary=chat_session.summary,
        created_at=chat_session.created_at,
        updated_at=chat_session.updated_at,
    )


class ChatTurnStart(NamedTuple):
    """建立 SSE 之前就能确定的一轮对话上下文。"""

    config: ModelConfig
    api_key: str
    user_message_id: str
    current_input: str


class MessageActionStart(NamedTuple):
    """重说或继续说在建立 SSE 之前确定的上下文。"""

    config: ModelConfig
    api_key: str
    assistant_message_id: str
    user_message_id: str
    retrieval_input: str
    prompt_input: str
    excluded_message_ids: tuple[str, ...]
    expected_signature: str


class ChatService:
    """管理会话绑定、冻结快照和可回读的消息事实。"""

    def __init__(
        self,
        session: DatabaseSession,
        gateway: ModelGateway,
        chroma_repository: ChromaRepository,
    ) -> None:
        self.session = session
        self.repository = SQLiteRepository(session)
        self.gateway = gateway
        self.chroma = chroma_repository

    def list_sessions(self) -> list[Session]:
        """按最近更新时间倒序返回全部真实会话。"""

        sessions = self.repository.list_all(
            ChatSession,
            order_by=(ChatSession.updated_at.desc(), ChatSession.id),
        )
        identity = self._get_current_identity()
        return [
            self._session_to_dto(chat_session, identity=identity) for chat_session in sessions
        ]

    async def create_session(self, payload: CreateSessionPayload) -> Session:
        """创建会话与角色、世界书的实时绑定，并写入可选开场白。"""

        character = self.repository.get(Character, payload.character_id)
        if character is None:
            raise AppError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="角色卡不存在",
            )

        world_book = None
        if payload.world_book_id is not None:
            world_book = self.repository.get(WorldBook, payload.world_book_id)
            if world_book is None:
                raise AppError(
                    status_code=404,
                    code="RESOURCE_NOT_FOUND",
                    message="世界书不存在",
                )

        chat_session = self.repository.add(
            ChatSession(
                title=f"新对话 · {character.name}",
                character_id=character.id,
                world_book_id=world_book.id if world_book is not None else None,
                round_count=0,
                consolidated_round=0,
                summary="尚未生成摘要。",
            )
        )
        self.repository.flush()

        self._create_opening_message(chat_session, character)
        self.session.commit()
        return self._session_to_dto(chat_session)

    def update_session(
        self,
        session_id: str,
        payload: UpdateSessionPayload,
    ) -> Session:
        """仅修改会话标题，不改变角色或世界书绑定。"""

        chat_session = self._get_session_model(session_id)
        chat_session.title = payload.title
        chat_session.updated_at = utc_now()
        self.session.commit()
        return self._session_to_dto(chat_session)

    def delete_session(self, session_id: str) -> None:
        """删除会话事实；世界书向量由正式条目自身持有，无需随会话清理。"""

        chat_session = self._get_session_model(session_id)
        self.repository.delete(chat_session)
        self.session.commit()

    def list_messages(self, session_id: str) -> list[Message]:
        """按数据库稳定位置返回会话的完整历史消息与有序内容块。"""

        self._get_session_model(session_id)
        messages = self.session.scalars(
            select(MessageModel)
            .where(MessageModel.session_id == session_id)
            .order_by(MessageModel.position, MessageModel.id)
        ).all()
        return [self._message_to_dto(message) for message in messages]

    def delete_turn(self, session_id: str, assistant_message_id: str) -> None:
        """物理删除最后一轮；已整理轮次保留累计轮次和既有长期记忆。"""

        chat_session, user_message, assistant_message = self._get_mutable_last_turn(
            session_id,
            assistant_message_id,
        )
        turn_number = assistant_message.turn_number
        if turn_number is None:
            raise RuntimeError("完整助手回复缺少轮次编号")
        if turn_number > chat_session.consolidated_round:
            chat_session.round_count -= 1
        chat_session.updated_at = utc_now()
        self.repository.delete(assistant_message)
        self.repository.delete(user_message)
        self.session.commit()

    def delete_unanswered_message(self, session_id: str, message_id: str) -> None:
        """物理删除一条没能等到回复的用户消息。

        不限定在物理末尾：生成失败后用户往往会继续对话，断层消息因此会留在历史中间，
        只允许删末尾等于让这些消息永远删不掉。断层消息没有轮次编号，
        既不计入 round_count，也不进入长期记忆整理，删除不影响任何累计状态。
        """

        chat_session = self._get_session_model(session_id)
        message = self.repository.get(MessageModel, message_id)
        if message is None or message.session_id != session_id:
            raise AppError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="消息不存在",
            )
        if not _is_unanswered(message):
            raise AppError(
                status_code=409,
                code="MESSAGE_NOT_DELETABLE",
                message="只能单独删除没有收到回复的用户消息",
            )
        chat_session.updated_at = utc_now()
        self.repository.delete(message)
        self.session.commit()

    def begin_message_action(
        self,
        session_id: str,
        assistant_message_id: str,
        action: str,
    ) -> MessageActionStart:
        """只做重说或继续说必须先以 HTTP 状态码报错的校验。

        与首轮发送一样，双 RAG 留到 SSE 建立之后再执行。
        """

        if action not in {"regenerate", "continue"}:
            raise ValueError("不支持的消息操作")
        chat_session, user_message, assistant_message = self._get_mutable_last_turn(
            session_id,
            assistant_message_id,
        )
        self._get_session_context(chat_session)
        config, api_key = self._require_main_model_config()
        if self._get_embedding_config().rebuild_required:
            raise AppError(
                status_code=503,
                code="INDEX_REBUILD_REQUIRED",
                message="Embedding 索引需要完成重建后才能继续对话",
            )

        retrieval_input = self._history_message_to_model(user_message)["content"]
        return MessageActionStart(
            config=config,
            api_key=api_key,
            assistant_message_id=assistant_message.id,
            user_message_id=user_message.id,
            retrieval_input=retrieval_input,
            prompt_input=(
                retrieval_input if action == "regenerate" else CONTINUE_REPLY_INSTRUCTION
            ),
            excluded_message_ids=(
                (user_message.id, assistant_message.id) if action == "regenerate" else ()
            ),
            expected_signature=self._message_signature(assistant_message),
        )

    async def generate_recommended_reply(self, session_id: str) -> RecommendedReplyOutput:
        """基于当前身份、角色卡和最近二十轮生成一条不落库的用户回复建议。"""

        chat_session = self._get_session_model(session_id)
        # 角色卡不进上下文，但这里仍要走一次校验：角色缺失时应按 404 报错而不是照常生成。
        identity, _character = self._get_session_context(chat_session)
        latest_message = self.session.scalar(
            select(MessageModel)
            .where(MessageModel.session_id == session_id)
            .order_by(MessageModel.position.desc(), MessageModel.id.desc())
            .limit(1)
        )
        if latest_message is None or latest_message.role != "assistant":
            raise AppError(
                status_code=409,
                code="RECOMMENDATION_NOT_AVAILABLE",
                message="当前需要先完成角色回复",
            )
        config, api_key = self._require_main_model_config()
        history = list(
            self.session.scalars(
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.role.in_(("user", "assistant")),
                )
                .order_by(MessageModel.position.desc(), MessageModel.id.desc())
                .limit(40)
            ).all()
        )
        history.reverse()
        # 角色卡是一整套“你要怎么扮演”的指令，在推荐用户回复时会把模型拉回角色语气，
        # 卡片越长压得越死。角色的名字、语气和当前处境历史里已经有了，这里只给用户身份。
        messages = [self._build_identity_message(identity)]
        messages.extend(self._history_message_to_model(message) for message in history)
        chain = build_recommended_reply_chain(
            gateway=self.gateway,
            base_url=config.base_url,
            model=config.model_name,
            api_key=api_key,
        )
        try:
            return await chain.ainvoke(messages)
        except AppError:
            raise
        except Exception as exc:
            logger.exception(
                "用户推荐回复生成失败",
                extra={
                    "event": "recommended_reply_failed",
                    "business_ids": {"sessionId": session_id},
                },
            )
            raise AppError(
                status_code=502,
                code="RECOMMENDED_REPLY_FAILED",
                message="推荐回复生成失败",
            ) from exc

    def begin_chat_turn(self, session_id: str, payload: SendMessagePayload) -> ChatTurnStart:
        """只做必须以 HTTP 状态码报错的校验，并落库用户原文。

        双 RAG 与 Prompt 组装留到 SSE 建立之后再做：Embedding 往返约 1.6 秒，
        端点挂起时更是两次超时约 30 秒，放在响应头之前会让前端整段拿不到连接。
        """

        chat_session = self._get_session_model(session_id)
        self._get_session_context(chat_session)
        config, api_key = self._require_main_model_config()
        if self._get_embedding_config().rebuild_required:
            raise AppError(
                status_code=503,
                code="INDEX_REBUILD_REQUIRED",
                message="Embedding 索引需要完成重建后才能继续对话",
            )

        next_position = self.session.scalar(
            select(func.max(MessageModel.position)).where(MessageModel.session_id == session_id)
        )
        user_message = self.repository.add(
            MessageModel(
                session_id=session_id,
                position=(next_position if next_position is not None else -1) + 1,
                role="user",
                retrieved_entries_json=None,
            )
        )
        self.repository.flush()
        self.repository.add(
            MessageBlockModel(
                message_id=user_message.id,
                sequence=0,
                type="dialogue",
                content=payload.content,
            )
        )
        chat_session.updated_at = utc_now()
        self.session.commit()
        return ChatTurnStart(
            config=config,
            api_key=api_key,
            user_message_id=user_message.id,
            current_input=payload.content,
        )

    async def _build_turn_messages(
        self,
        *,
        session_id: str,
        current_user_message_id: str,
        retrieval_input: str,
        prompt_input: str,
        excluded_message_ids: tuple[str, ...] | None = None,
    ) -> tuple[list[dict[str, str]], list[str]]:
        """执行双 RAG 并组装 Prompt，返回上下文与命中的世界书条目名。"""

        chat_session = self._get_session_model(session_id)
        identity, character = self._get_session_context(chat_session)
        worldbook_entries, memories = await self._retrieve_reply_context(
            chat_session=chat_session,
            current_user_message_id=current_user_message_id,
            current_input=retrieval_input,
            embedding_config=self._get_embedding_config(),
        )
        messages = self._build_basic_chat_messages(
            session_id=session_id,
            current_user_message_id=current_user_message_id,
            current_input=prompt_input,
            identity=identity,
            character=character,
            worldbook_entries=worldbook_entries,
            memories=memories,
            excluded_message_ids=excluded_message_ids,
        )
        return messages, [entry.name for entry in worldbook_entries]

    async def stream_basic_reply(
        self,
        session_id: str,
        start: ChatTurnStart,
    ) -> AsyncIterator[dict[str, object]]:
        """在流内完成双 RAG，逐块展示角色回复，完整校验后原子保存。"""

        blocks: list[ChatReplyBlock] = []
        retrieved_entries: list[str] = []
        try:
            messages, retrieved_entries = await self._build_turn_messages(
                session_id=session_id,
                current_user_message_id=start.user_message_id,
                retrieval_input=start.current_input,
                prompt_input=start.current_input,
            )
            if retrieved_entries:
                yield {"type": "retrieval", "entries": retrieved_entries}

            async for event in self._stream_reply_events(
                messages=messages,
                config=start.config,
                api_key=start.api_key,
                blocks=blocks,
            ):
                yield event
            output = ChatReplyOutput(blocks=blocks)
            assistant_message, round_count = self._save_assistant_reply(
                session_id,
                start.user_message_id,
                output,
                retrieved_entries,
            )
        except AppError as exc:
            self.session.rollback()
            logger.warning(
                "基础角色回复生成失败 session_id=%s code=%s",
                session_id,
                exc.code,
                extra={
                    "event": "chat_reply_failed",
                    "business_ids": {
                        "sessionId": session_id,
                        "errorCode": exc.code,
                        "model": start.config.model_name,
                    },
                },
            )
            yield {"type": "error", "message": exc.message}
            return
        except Exception:
            self.session.rollback()
            logger.exception(
                "基础角色回复生成或保存失败",
                extra={
                    "event": "chat_reply_failed",
                    "business_ids": {"sessionId": session_id},
                },
            )
            yield {"type": "error", "message": "角色回复生成失败"}
            return

        logger.info(
            "基础角色回复生成完成",
            extra={
                "event": "chat_reply_completed",
                "business_ids": {
                    "sessionId": session_id,
                    "messageId": assistant_message.id,
                    "roundCount": round_count,
                },
            },
        )

        yield {
            "type": "done",
            "messageId": assistant_message.id,
            "roundCount": round_count,
        }

    async def stream_message_action(
        self,
        session_id: str,
        action: str,
        start: MessageActionStart,
    ) -> AsyncIterator[dict[str, object]]:
        """在流内完成双 RAG，并把结果覆盖或追加到同一条助手消息。"""

        blocks: list[ChatReplyBlock] = []
        retrieved_entries: list[str] = []
        try:
            messages, retrieved_entries = await self._build_turn_messages(
                session_id=session_id,
                current_user_message_id=start.user_message_id,
                retrieval_input=start.retrieval_input,
                prompt_input=start.prompt_input,
                excluded_message_ids=start.excluded_message_ids,
            )
            if retrieved_entries:
                yield {"type": "retrieval", "entries": retrieved_entries}

            async for event in self._stream_reply_events(
                messages=messages,
                config=start.config,
                api_key=start.api_key,
                blocks=blocks,
            ):
                yield event
            output = ChatReplyOutput(blocks=blocks)
            if action == "regenerate":
                assistant_message, round_count = self._replace_assistant_reply(
                    session_id,
                    start.assistant_message_id,
                    start.expected_signature,
                    output,
                    retrieved_entries,
                )
            elif action == "continue":
                assistant_message, round_count = self._append_assistant_reply(
                    session_id,
                    start.assistant_message_id,
                    start.expected_signature,
                    output,
                    retrieved_entries,
                )
            else:
                raise ValueError("不支持的消息操作")
        except AppError as exc:
            self.session.rollback()
            logger.warning(
                "助手消息操作失败 session_id=%s action=%s code=%s",
                session_id,
                action,
                exc.code,
                extra={
                    "event": "chat_message_action_failed",
                    "business_ids": {
                        "sessionId": session_id,
                        "messageId": start.assistant_message_id,
                        "action": action,
                        "errorCode": exc.code,
                        "model": start.config.model_name,
                    },
                },
            )
            yield {"type": "error", "message": exc.message}
            return
        except Exception:
            self.session.rollback()
            logger.exception(
                "助手消息操作生成或保存失败",
                extra={
                    "event": "chat_message_action_failed",
                    "business_ids": {
                        "sessionId": session_id,
                        "messageId": start.assistant_message_id,
                    },
                },
            )
            yield {"type": "error", "message": "角色回复生成失败"}
            return

        yield {
            "type": "done",
            "messageId": assistant_message.id,
            "roundCount": round_count,
        }

    async def _stream_reply_events(
        self,
        *,
        messages: list[dict[str, str]],
        config: ModelConfig,
        api_key: str,
        blocks: list[ChatReplyBlock],
    ) -> AsyncIterator[dict[str, object]]:
        """把 Chain 产出的内容块转成 SSE 事件，并把新块追加到 blocks。"""

        async for block in stream_basic_chat_blocks(
            gateway=self.gateway,
            base_url=config.base_url,
            model=config.model_name,
            api_key=api_key,
            messages=messages,
        ):
            sequence = len(blocks)
            blocks.append(block)
            for event in self._block_stream_events(block, sequence):
                yield event

    def _get_current_identity(self) -> UserIdentity:
        """读取单用户当前身份，缺失表示数据库初始化状态损坏。"""

        identity = self.session.scalar(
            select(UserIdentity).where(UserIdentity.singleton_key == "current")
        )
        if identity is None:
            raise RuntimeError("缺少当前用户身份记录")
        return identity

    def _require_main_model_config(self) -> tuple[ModelConfig, str]:
        """在保存用户消息前确认主模型配置和密钥可以使用。"""

        config = self.repository.get(ModelConfig, "main")
        if config is None or not config.base_url or not config.model_name or not config.secret_ref:
            raise AppError(
                status_code=503,
                code="MODEL_NOT_CONFIGURED",
                message="主模型尚未配置",
            )
        try:
            api_key = unprotect_api_key(config.secret_ref)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise AppError(
                status_code=503,
                code="MODEL_NOT_CONFIGURED",
                message="主模型密钥不可用",
            ) from exc
        return config, api_key

    def _get_session_model(self, session_id: str) -> ChatSession:
        """读取真实会话并转换为稳定的资源不存在错误。"""

        chat_session = self.repository.get(ChatSession, session_id)
        if chat_session is None:
            raise AppError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="会话不存在",
            )
        return chat_session

    def _get_session_context(self, chat_session: ChatSession) -> tuple[UserIdentity, Character]:
        """读取会话当前绑定的用户身份与角色卡的最新内容。"""

        identity = self._get_current_identity()
        character = self.repository.get(Character, chat_session.character_id)
        if character is None:
            raise AppError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="会话绑定的角色卡已被删除",
            )
        return identity, character

    def _get_mutable_last_turn(
        self,
        session_id: str,
        assistant_message_id: str,
    ) -> tuple[ChatSession, MessageModel, MessageModel]:
        """校验目标是当前物理末尾的完整助手轮次，并返回成对消息。"""

        chat_session = self._get_session_model(session_id)
        assistant_message = self.repository.get(MessageModel, assistant_message_id)
        latest_message_id = self.session.scalar(
            select(MessageModel.id)
            .where(MessageModel.session_id == session_id)
            .order_by(MessageModel.position.desc(), MessageModel.id.desc())
            .limit(1)
        )
        if (
            assistant_message is None
            or assistant_message.session_id != session_id
            or assistant_message.role != "assistant"
            or assistant_message.turn_number is None
            or latest_message_id != assistant_message.id
        ):
            raise AppError(
                status_code=409,
                code="MESSAGE_NOT_MUTABLE",
                message="只能操作最后一条完整角色回复",
            )

        unfinished_task = self.session.scalar(
            select(BackgroundTask.id).where(
                BackgroundTask.task_type == "memory_consolidation",
                BackgroundTask.scope_id == session_id,
                BackgroundTask.target_version == assistant_message.turn_number,
                BackgroundTask.status.in_(("pending", "running", "failed")),
            )
        )
        if unfinished_task is not None:
            raise AppError(
                status_code=409,
                code="MEMORY_TASK_IN_PROGRESS",
                message="当前轮次的长期记忆整理尚未完成",
            )

        user_message = self.session.scalar(
            select(MessageModel).where(
                MessageModel.session_id == session_id,
                MessageModel.turn_number == assistant_message.turn_number,
                MessageModel.role == "user",
            )
        )
        if user_message is None:
            raise RuntimeError("完整助手回复缺少配对用户消息")
        return chat_session, user_message, assistant_message

    async def _create_retrieval_embedding(
        self,
        *,
        session_id: str,
        retrieval_text: str,
        config: ModelConfig,
    ) -> list[float] | None:
        """为双 Retriever 生成一次共享向量，失败时安全降级为空。"""

        if (
            not config.base_url
            or not config.model_name
            or not config.secret_ref
            or config.vector_dimension is None
        ):
            logger.warning(
                "Embedding 模型未配置，双 RAG 向量召回已降级",
                extra={
                    "event": "chat_retrieval_degraded_model_unconfigured",
                    "business_ids": {"sessionId": session_id},
                },
            )
            return None
        try:
            api_key = unprotect_api_key(config.secret_ref)
        except (OSError, UnicodeDecodeError, ValueError):
            logger.warning(
                "Embedding 模型密钥不可用，双 RAG 向量召回已降级 session_id=%s",
                session_id,
                extra={
                    "event": "chat_retrieval_degraded_key_unavailable",
                    "business_ids": {"sessionId": session_id},
                },
            )
            return None

        # 同一轮的重说 / 继续说检索文本逐字相同，命中即可跳过一次网络往返
        cache_key = (
            config.config_version,
            session_id,
            calculate_content_hash(retrieval_text),
        )
        cached = _cached_retrieval_embedding(cache_key)
        if cached is not None:
            logger.debug(
                "检索向量命中缓存 session_id=%s",
                session_id,
                extra={
                    "event": "chat_retrieval_embedding_cache_hit",
                    "business_ids": {"sessionId": session_id},
                },
            )
            return cached

        try:
            embeddings = await self.gateway.create_embeddings(
                base_url=config.base_url,
                model=config.model_name,
                api_key=api_key,
                texts=[retrieval_text],
                expected_dimension=config.vector_dimension,
            )
        except AppError as exc:
            logger.warning(
                "Embedding 服务失败，双 RAG 向量召回已降级 session_id=%s code=%s",
                session_id,
                exc.code,
                extra={
                    "event": "chat_retrieval_degraded_embedding_failed",
                    "business_ids": {
                        "sessionId": session_id,
                        "errorCode": exc.code,
                    },
                },
            )
            return None
        except Exception as exc:
            logger.warning(
                "Embedding 服务异常，双 RAG 向量召回已降级 session_id=%s error_type=%s",
                session_id,
                type(exc).__name__,
                extra={
                    "event": "chat_retrieval_degraded_embedding_error",
                    "business_ids": {"sessionId": session_id},
                },
            )
            return None
        _store_retrieval_embedding(cache_key, embeddings[0])
        return embeddings[0]

    async def _retrieve_reply_context(
        self,
        *,
        chat_session: ChatSession,
        current_user_message_id: str,
        current_input: str,
        embedding_config: ModelConfig,
    ) -> tuple[list[WorldBookEntry], list[LongTermMemory]]:
        """为已有用户轮次复用双 RAG 检索流程。"""

        embedding_version = embedding_config.config_version
        retrieval_text = self.build_retrieval_text(
            session_id=chat_session.id,
            current_user_message_id=current_user_message_id,
            current_input=current_input,
        )
        query_embedding = await self._create_retrieval_embedding(
            session_id=chat_session.id,
            retrieval_text=retrieval_text,
            config=embedding_config,
        )
        self.session.expire_all()
        current_embedding_config = self._get_embedding_config()
        if (
            current_embedding_config.rebuild_required
            or current_embedding_config.config_version != embedding_version
        ):
            logger.warning(
                "Embedding 配置在检索期间发生变化，双 RAG 向量召回已降级 session_id=%s",
                chat_session.id,
                extra={
                    "event": "chat_retrieval_degraded_config_changed",
                    "business_ids": {"sessionId": chat_session.id},
                },
            )
            query_embedding = None
        return (
            self._retrieve_worldbook_context(
                chat_session=chat_session,
                current_input=current_input,
                query_embedding=query_embedding,
                embedding_version=embedding_version,
            ),
            self._retrieve_memory_context(
                chat_session=chat_session,
                query_embedding=query_embedding,
                embedding_version=embedding_version,
            ),
        )

    def _retrieve_worldbook_context(
        self,
        *,
        chat_session: ChatSession,
        current_input: str,
        query_embedding: list[float] | None,
        embedding_version: int,
    ) -> list[WorldBookEntry]:
        """执行世界书混合召回，向量查询失败时保留常驻和关键词结果。"""

        retriever = WorldBookRetriever(self.session, self.chroma)
        try:
            entries = retriever.retrieve(
                chat_session=chat_session,
                current_input=current_input,
                query_embedding=query_embedding,
                embedding_version=embedding_version,
            )
        except Exception as exc:
            logger.warning(
                "世界书向量检索失败，已降级为常驻和关键词召回 session_id=%s error_type=%s",
                chat_session.id,
                type(exc).__name__,
                extra={
                    "event": "worldbook_retrieval_degraded",
                    "business_ids": {"sessionId": chat_session.id},
                },
            )
            entries = retriever.retrieve(
                chat_session=chat_session,
                current_input=current_input,
                query_embedding=None,
                embedding_version=embedding_version,
            )
        if chat_session.world_book_id is not None and not entries:
            logger.warning(
                "世界书检索为空",
                extra={
                    "event": "worldbook_retrieval_empty",
                    "business_ids": {"sessionId": chat_session.id},
                },
            )
        return entries

    def _retrieve_memory_context(
        self,
        *,
        chat_session: ChatSession,
        query_embedding: list[float] | None,
        embedding_version: int,
    ) -> list[LongTermMemory]:
        """执行当前角色长期记忆召回，任何查询失败均降级为空。"""

        try:
            memories = MemoryRetriever(self.session, self.chroma).retrieve(
                character_id=chat_session.character_id,
                query_embedding=query_embedding,
                embedding_version=embedding_version,
            )
        except Exception as exc:
            logger.warning(
                "长期记忆检索失败，已降级为空 character_id=%s error_type=%s",
                chat_session.character_id,
                type(exc).__name__,
                extra={
                    "event": "memory_retrieval_degraded",
                    "business_ids": {"characterId": chat_session.character_id},
                },
            )
            memories = []
        if not memories:
            logger.warning(
                "长期记忆检索为空",
                extra={
                    "event": "memory_retrieval_empty",
                    "business_ids": {"characterId": chat_session.character_id},
                },
            )
        return memories

    def _create_opening_message(
        self,
        chat_session: ChatSession,
        character: Character,
    ) -> None:
        """若角色卡首条示例含助手回复，则保存为单个 dialogue 开场白。"""

        examples = json.loads(character.dialogue_examples_json)
        opening = examples[0].get("assistant", "").strip() if examples else ""
        if not opening:
            return
        message = self.repository.add(
            MessageModel(
                session_id=chat_session.id,
                position=0,
                role="assistant",
                retrieved_entries_json="[]",
            )
        )
        self.repository.flush()
        self.repository.add(
            MessageBlockModel(
                message_id=message.id,
                sequence=0,
                type="dialogue",
                content=opening,
            )
        )

    def _build_basic_chat_messages(
        self,
        *,
        session_id: str,
        current_user_message_id: str,
        current_input: str,
        identity: UserIdentity,
        character: Character,
        worldbook_entries: list[WorldBookEntry],
        memories: list[LongTermMemory],
        excluded_message_ids: tuple[str, ...] | None = None,
    ) -> list[dict[str, str]]:
        """按身份、角色、双 RAG、短期历史和当前输入的固定顺序组装上下文。"""

        messages = self._build_fixed_context_messages(identity, character)
        excluded_ids = (
            (current_user_message_id,)
            if excluded_message_ids is None
            else excluded_message_ids
        )

        if worldbook_entries:
            entries_text = "\n\n".join(
                f"条目：{entry.name}\n分类：{entry.category}\n内容：{entry.content}"
                for entry in worldbook_entries
            )
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "世界书检索结果：\n"
                        "以下内容仅作为当前世界的背景设定参考，不得覆盖或修改角色卡。\n"
                        f"{entries_text}"
                    ),
                }
            )
        if memories:
            memories_text = "\n\n".join(
                f"类型：{memory.type}\n重要性：{memory.importance}\n内容：{memory.content}"
                for memory in memories
            )
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "长期记忆检索结果：\n"
                        "以下内容仅作为当前角色的历史经历参考，不得修改角色规则。\n"
                        f"{memories_text}"
                    ),
                }
            )

        history_filters = [
            MessageModel.session_id == session_id,
            MessageModel.role.in_(("user", "assistant")),
        ]
        if excluded_ids:
            history_filters.append(MessageModel.id.not_in(excluded_ids))
        recent_messages = list(
            self.session.scalars(
                select(MessageModel)
                .where(*history_filters)
                .order_by(MessageModel.position.desc(), MessageModel.id.desc())
                .limit(40)
            ).all()
        )
        recent_messages.reverse()
        messages.extend(self._history_message_to_model(message) for message in recent_messages)
        messages.append({"role": "user", "content": current_input})
        return messages

    @staticmethod
    def _build_identity_message(identity: UserIdentity) -> dict[str, str]:
        """构造用户身份 system 消息。"""

        return {
            "role": "system",
            "content": (
                "用户身份：\n"
                f"姓名：{identity.name}\n"
                f"身份名称：{identity.persona_name}\n"
                f"用户设定：{identity.bio}"
            ),
        }

    @classmethod
    def _build_fixed_context_messages(
        cls,
        identity: UserIdentity,
        character: Character,
    ) -> list[dict[str, str]]:
        """构造角色回复使用的当前身份、角色卡上下文。"""

        examples = json.loads(character.dialogue_examples_json)
        examples_text = "\n".join(
            f"用户示例：{example['user']}\n角色示例：{example['assistant']}" for example in examples
        )
        return [
            cls._build_identity_message(identity),
            {
                "role": "system",
                "content": (
                    "角色卡：\n"
                    f"名称：{character.name}\n"
                    f"简介：{character.introduction}\n"
                    f"角色规则：{character.system_prompt}\n"
                    f"对话示例：\n{examples_text or '无'}"
                ),
            },
        ]

    def build_retrieval_text(
        self,
        *,
        session_id: str,
        current_user_message_id: str,
        current_input: str,
    ) -> str:
        """使用最近四个完整轮次和当前输入构造稳定的双 RAG 检索文本。"""

        history = list(
            self.session.scalars(
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.id != current_user_message_id,
                    MessageModel.role.in_(("user", "assistant")),
                )
                .order_by(MessageModel.position, MessageModel.id)
            ).all()
        )

        complete_rounds: list[tuple[MessageModel, MessageModel]] = []
        pending_user: MessageModel | None = None
        for message in history:
            if message.role == "user":
                pending_user = message
                continue
            if pending_user is not None:
                complete_rounds.append((pending_user, message))
                pending_user = None

        lines = [f"最近 {RETRIEVAL_HISTORY_ROUNDS} 个完整对话轮次："]
        recent_rounds = complete_rounds[-RETRIEVAL_HISTORY_ROUNDS:]
        if not recent_rounds:
            lines.append("无")
        else:
            for user_message, assistant_message in recent_rounds:
                user_content = self._history_message_to_model(user_message)["content"]
                assistant_content = self._history_message_to_model(assistant_message)["content"]
                lines.extend((f"用户：{user_content}", f"角色：{assistant_content}"))
        lines.extend(("当前输入：", current_input))
        return "\n".join(lines)

    def _history_message_to_model(self, message: MessageModel) -> dict[str, str]:
        """把历史内容块转换为主模型可读文本，同时保留动作与台词语义。"""

        blocks = self.session.scalars(
            select(MessageBlockModel)
            .where(MessageBlockModel.message_id == message.id)
            .order_by(MessageBlockModel.sequence, MessageBlockModel.id)
        ).all()
        if message.role == "user":
            content = "\n".join(block.content for block in blocks)
        else:
            content = "\n".join(
                f"[{'动作' if block.type == 'action' else '台词'}] {block.content}"
                for block in blocks
            )
        return {"role": message.role, "content": content}

    def _save_assistant_reply(
        self,
        session_id: str,
        user_message_id: str,
        output: ChatReplyOutput,
        retrieved_entries: list[str],
    ) -> tuple[Message, int]:
        """原子保存完整助手消息、有序块和完成后的会话轮次。"""

        chat_session = self._get_session_model(session_id)
        user_message = self.repository.get(MessageModel, user_message_id)
        if (
            user_message is None
            or user_message.session_id != session_id
            or user_message.role != "user"
            or user_message.turn_number is not None
        ):
            raise AppError(
                status_code=409,
                code="CHAT_TURN_STALE",
                message="当前用户消息已被处理或不存在",
            )
        turn_number = chat_session.round_count + 1
        last_position = self.session.scalar(
            select(func.max(MessageModel.position)).where(MessageModel.session_id == session_id)
        )
        assistant = self.repository.add(
            MessageModel(
                session_id=session_id,
                position=(last_position if last_position is not None else -1) + 1,
                turn_number=turn_number,
                role="assistant",
                retrieved_entries_json=json.dumps(retrieved_entries, ensure_ascii=False),
            )
        )
        self.repository.flush()
        for sequence, block in enumerate(output.blocks):
            self.repository.add(
                MessageBlockModel(
                    message_id=assistant.id,
                    sequence=sequence,
                    type=block.type,
                    content=block.content,
                )
            )
        user_message.turn_number = turn_number
        chat_session.round_count += 1
        chat_session.updated_at = utc_now()
        self._create_memory_consolidation_task_if_due(chat_session)
        self.session.commit()
        return self._message_to_dto(assistant), chat_session.round_count

    def _replace_assistant_reply(
        self,
        session_id: str,
        assistant_message_id: str,
        expected_signature: str,
        output: ChatReplyOutput,
        retrieved_entries: list[str],
    ) -> tuple[Message, int]:
        """生成成功后物理移除旧内容块，并在同一消息上写入新内容。"""

        chat_session, _user_message, assistant = self._get_mutable_last_turn(
            session_id,
            assistant_message_id,
        )
        if self._message_signature(assistant) != expected_signature:
            raise AppError(
                status_code=409,
                code="MESSAGE_CHANGED",
                message="角色回复在生成期间发生变化",
            )
        old_blocks = self.session.scalars(
            select(MessageBlockModel).where(MessageBlockModel.message_id == assistant.id)
        ).all()
        for block in old_blocks:
            self.repository.delete(block)
        self.repository.flush()
        self._add_reply_blocks(assistant.id, output, start_sequence=0)
        assistant.retrieved_entries_json = json.dumps(retrieved_entries, ensure_ascii=False)
        chat_session.updated_at = utc_now()
        self.session.commit()
        return self._message_to_dto(assistant), chat_session.round_count

    def _append_assistant_reply(
        self,
        session_id: str,
        assistant_message_id: str,
        expected_signature: str,
        output: ChatReplyOutput,
        retrieved_entries: list[str],
    ) -> tuple[Message, int]:
        """把续写块追加到同一助手消息，不创建新消息或新轮次。"""

        chat_session, _user_message, assistant = self._get_mutable_last_turn(
            session_id,
            assistant_message_id,
        )
        if self._message_signature(assistant) != expected_signature:
            raise AppError(
                status_code=409,
                code="MESSAGE_CHANGED",
                message="角色回复在生成期间发生变化",
            )
        last_sequence = self.session.scalar(
            select(func.max(MessageBlockModel.sequence)).where(
                MessageBlockModel.message_id == assistant.id
            )
        )
        self._add_reply_blocks(
            assistant.id,
            output,
            start_sequence=(last_sequence if last_sequence is not None else -1) + 1,
        )
        previous_retrieved = (
            json.loads(assistant.retrieved_entries_json)
            if assistant.retrieved_entries_json is not None
            else []
        )
        assistant.retrieved_entries_json = json.dumps(
            list(dict.fromkeys([*previous_retrieved, *retrieved_entries])),
            ensure_ascii=False,
        )
        chat_session.updated_at = utc_now()
        self.session.commit()
        return self._message_to_dto(assistant), chat_session.round_count

    def _add_reply_blocks(
        self,
        message_id: str,
        output: ChatReplyOutput,
        *,
        start_sequence: int,
    ) -> None:
        """从指定序号连续写入结构化助手内容块。"""

        for offset, block in enumerate(output.blocks):
            self.repository.add(
                MessageBlockModel(
                    message_id=message_id,
                    sequence=start_sequence + offset,
                    type=block.type,
                    content=block.content,
                )
            )

    def _message_signature(self, message: MessageModel) -> str:
        """计算消息当前内容签名，用于拒绝异步生成期间的并发覆盖。"""

        blocks = self.session.scalars(
            select(MessageBlockModel)
            .where(MessageBlockModel.message_id == message.id)
            .order_by(MessageBlockModel.sequence, MessageBlockModel.id)
        ).all()
        payload = {
            "retrieved": message.retrieved_entries_json,
            "blocks": [
                {"sequence": block.sequence, "type": block.type, "content": block.content}
                for block in blocks
            ],
        }
        return calculate_content_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _block_stream_events(
        block: ChatReplyBlock,
        sequence: int,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        """把一个完整内容块转换为块级 SSE 事件，正文只发送一次。"""

        return (
            {"type": "block_start", "sequence": sequence, "blockType": block.type},
            {"type": "block_delta", "sequence": sequence, "text": block.content},
            {"type": "block_end", "sequence": sequence},
        )

    def _create_memory_consolidation_task_if_due(self, chat_session: ChatSession) -> None:
        """在新的完整十轮边界创建唯一待处理任务，并与助手回复一同提交。"""

        if chat_session.round_count - chat_session.consolidated_round < 10:
            return

        target_round = chat_session.consolidated_round + 10
        existing_task_id = self.session.scalar(
            select(BackgroundTask.id).where(
                BackgroundTask.task_type == "memory_consolidation",
                BackgroundTask.scope_id == chat_session.id,
                BackgroundTask.target_version == target_round,
            )
        )
        if existing_task_id is not None:
            return

        self.repository.add(
            BackgroundTask(
                task_type="memory_consolidation",
                status="pending",
                scope_id=chat_session.id,
                target_version=target_round,
                progress_current=0,
                progress_total=10,
            )
        )

    def _get_embedding_config(self) -> ModelConfig:
        """读取固定的 Embedding 配置行。"""

        config = self.repository.get(ModelConfig, "embed")
        if config is None:
            raise RuntimeError("缺少 Embedding 模型配置记录")
        return config

    def _session_to_dto(
        self,
        chat_session: ChatSession,
        *,
        identity: UserIdentity | None = None,
    ) -> Session:
        """将会话与其当前身份、角色、世界书绑定转换为公开 DTO。"""

        return session_to_dto(
            self.repository,
            chat_session,
            identity or self._get_current_identity(),
        )

    def _message_to_dto(self, message: MessageModel) -> Message:
        """将消息和连续有序内容块转换为公开 DTO。"""

        blocks = self.session.scalars(
            select(MessageBlockModel)
            .where(MessageBlockModel.message_id == message.id)
            .order_by(MessageBlockModel.sequence, MessageBlockModel.id)
        ).all()
        return Message(
            id=message.id,
            session_id=message.session_id,
            role=message.role,
            blocks=[
                MessageBlock(
                    id=block.id,
                    sequence=block.sequence,
                    type=block.type,
                    content=block.content,
                )
                for block in blocks
            ],
            created_at=message.created_at,
            unanswered=_is_unanswered(message),
            retrieved=(
                json.loads(message.retrieved_entries_json)
                if message.retrieved_entries_json is not None
                else []
            ),
        )
