"""阶段 5–7 会话、双 RAG 与长期记忆全链路服务测试。"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import protect_api_key
from app.db.database import create_database_engine, create_session_factory
from app.db.models import (
    BackgroundTask,
    Character,
    ChatSession,
    LongTermMemory,
    Message,
    MessageBlock,
    ModelConfig,
    SessionContextSnapshot,
    SessionWorldBookEntrySnapshot,
    UserIdentity,
    WorldBook,
    WorldBookEntry,
)
from app.repositories.chroma_repository import (
    build_worldbook_index_text,
    calculate_content_hash,
    memory_document_id,
    session_entry_document_id,
)
from app.schemas.dto import (
    CreateSessionPayload,
    SendMessagePayload,
    UpdateSessionPayload,
)
from app.services.chat_service import ChatService
from app.tasks.memory_task import MemoryTask
from sqlalchemy import func, select

from scripts.init_db import initialize_database


class StubGateway:
    """为会话世界书快照返回固定向量或受控错误。"""

    def __init__(
        self,
        *,
        error: AppError | None = None,
        chat_response: str = '{"blocks":[{"type":"dialogue","content":"收到。"}]}',
        chat_error: AppError | None = None,
    ) -> None:
        self.error = error
        self.chat_response = chat_response
        self.chat_error = chat_error
        self.request: dict[str, object] | None = None
        self.embedding_requests: list[dict[str, object]] = []
        self.chat_requests: list[list[dict[str, str]]] = []

    async def create_embeddings(self, **kwargs) -> list[list[float]]:
        """记录 Embedding 请求，并返回三维向量。"""

        self.request = kwargs
        self.embedding_requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return [[0.1, 0.2, 0.3] for _text in kwargs["texts"]]

    async def create_chat_completion(self, **kwargs) -> str:
        """记录非流式模型上下文，并返回或抛出受控主模型结果。"""

        self.chat_requests.append(kwargs["messages"])
        if self.chat_error is not None:
            raise self.chat_error
        return self.chat_response

    async def stream_chat_completion(self, **kwargs):
        """记录交互对话上下文，并返回一个完整的测试网络分片。"""

        self.chat_requests.append(kwargs["messages"])
        if self.chat_error is not None:
            raise self.chat_error
        yield self.chat_response


class RecordingChroma:
    """记录会话快照向量写入与删除。"""

    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []
        self.deleted_ids: list[str] = []
        self.worldbook_queries: list[dict[str, object]] = []
        self.memory_queries: list[dict[str, object]] = []
        self.worldbook_result: dict[str, object] = {
            "ids": [[]],
            "distances": [[]],
            "metadatas": [[]],
        }
        self.memory_result: dict[str, object] = {
            "ids": [[]],
            "distances": [[]],
            "metadatas": [[]],
        }
        self.worldbook_error: Exception | None = None
        self.memory_error: Exception | None = None

    def upsert_worldbook(self, **kwargs) -> None:
        """记录世界书 Collection 写入。"""

        self.upserts.append(kwargs)

    def delete_worldbook(self, *, ids: list[str]) -> None:
        """记录确定 ID 删除。"""

        self.deleted_ids.extend(ids)

    def query_worldbook(self, **kwargs) -> dict[str, object]:
        """记录世界书向量查询并返回受控结果。"""

        self.worldbook_queries.append(kwargs)
        if self.worldbook_error is not None:
            raise self.worldbook_error
        return self.worldbook_result

    def query_memories(self, **kwargs) -> dict[str, object]:
        """记录长期记忆向量查询并返回受控结果。"""

        self.memory_queries.append(kwargs)
        if self.memory_error is not None:
            raise self.memory_error
        return self.memory_result

    def upsert_memories(self, **kwargs) -> None:
        """记录长期记忆 Collection 写入。"""

        self.upserts.append(kwargs)

    def delete_memories(self, *, ids: list[str]) -> None:
        """记录长期记忆确定 ID 删除。"""

        self.deleted_ids.extend(ids)


def create_service(tmp_path: Path, gateway=None, chroma=None):
    """创建使用隔离 SQLite 与记录型 Chroma 的会话服务。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session = create_session_factory(engine)()
    resolved_chroma = chroma or RecordingChroma()
    return (
        ChatService(session, gateway or StubGateway(), resolved_chroma),
        session,
        engine,
        resolved_chroma,
    )


def configure_embedding(session, *, version: int = 2) -> None:
    """配置可用的三维 Embedding 测试端点。"""

    config = session.get(ModelConfig, "embed")
    assert config is not None
    config.base_url = "https://models.example/v1"
    config.model_name = "embed-model"
    config.secret_ref = protect_api_key("sk-embed-test")
    config.vector_dimension = 3
    config.config_version = version
    session.commit()


def configure_main_model(session) -> None:
    """配置可用的主模型测试端点。"""

    config = session.get(ModelConfig, "main")
    assert config is not None
    config.base_url = "https://models.example/v1"
    config.model_name = "chat-model"
    config.secret_ref = protect_api_key("sk-main-test")
    session.commit()


def add_character(session, *, name: str = "艾拉") -> Character:
    """创建带开场白示例的真实角色卡。"""

    character = Character(
        name=name,
        introduction="旧型号仿生人偶",
        system_prompt="始终以艾拉的身份回应。",
        dialogue_examples_json=json.dumps(
            [{"user": "你好", "assistant": "你终于来了。"}],
            ensure_ascii=False,
        ),
    )
    session.add(character)
    session.commit()
    return character


def add_world_book_with_entries(session) -> tuple[WorldBook, list[WorldBookEntry]]:
    """创建包含一个启用条目和一个停用条目的真实世界书。"""

    book = WorldBook(name="魔法人偶", raw_content="完整原文")
    session.add(book)
    session.flush()
    entries: list[WorldBookEntry] = []
    for position, enabled, name in ((0, 1, "银港"), (1, 0, "废弃设定")):
        text = build_worldbook_index_text(
            name=name,
            category="地点",
            keywords=[name],
            content=f"{name}的事实。",
        )
        entry = WorldBookEntry(
            world_book_id=book.id,
            position=position,
            name=name,
            content=f"{name}的事实。",
            keywords_json=json.dumps([name], ensure_ascii=False),
            category="地点",
            resident=position == 0,
            enabled=enabled,
            index_status="ready",
            embedding_version=2,
            content_hash=calculate_content_hash(text),
        )
        session.add(entry)
        entries.append(entry)
    session.commit()
    return book, entries


def test_create_session_freezes_context_and_indexes_only_enabled_entries(
    tmp_path: Path,
) -> None:
    """会话创建冻结身份、角色与已启用世界书条目，并保存开场白。"""

    gateway = StubGateway()
    service, session, engine, chroma = create_service(tmp_path, gateway=gateway)
    try:
        configure_embedding(session)
        identity = session.scalar(
            select(UserIdentity).where(UserIdentity.singleton_key == "current")
        )
        assert identity is not None
        identity.name = "Strand"
        identity.persona_name = "master"
        character = add_character(session)
        book, entries = add_world_book_with_entries(session)
        session.commit()

        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=book.id)
            )
        )

        assert created.character_id == character.id
        assert created.world_book_id == book.id
        assert created.identity_name == "Strand"
        assert created.identity_persona_name == "master"
        assert created.character_name == "艾拉"
        assert created.world_book_name == "魔法人偶"
        assert created.round_count == 0
        context = session.scalar(
            select(SessionContextSnapshot).where(
                SessionContextSnapshot.session_id == created.id
            )
        )
        assert context is not None
        assert context.id == created.identity_snapshot_id
        assert context.identity_name == "Strand"
        assert context.character_name == "艾拉"
        assert context.world_book_name == "魔法人偶"

        snapshots = list(
            session.scalars(
                select(SessionWorldBookEntrySnapshot).where(
                    SessionWorldBookEntrySnapshot.context_snapshot_id == context.id
                )
            ).all()
        )
        assert [snapshot.source_entry_id for snapshot in snapshots] == [entries[0].id]
        assert snapshots[0].index_status == "ready"
        assert snapshots[0].embedding_version == 2
        assert gateway.request is not None
        assert gateway.request["texts"] == [
            "名称：银港\n分类：地点\n关键词：银港\n内容：银港的事实。"
        ]
        assert chroma.upserts[0]["ids"] == [session_entry_document_id(snapshots[0].id)]
        assert chroma.upserts[0]["metadatas"][0]["sessionId"] == created.id
        assert chroma.upserts[0]["metadatas"][0]["sourceKind"] == "sessionSnapshot"

        messages = service.list_messages(created.id)
        assert len(messages) == 1
        assert messages[0].role == "assistant"
        assert messages[0].blocks[0].content == "你终于来了。"

        identity.name = "修改后的身份"
        character.name = "修改后的角色"
        book.name = "修改后的世界书"
        entries[0].content = "修改后的条目"
        session.commit()
        session.refresh(context)
        session.refresh(snapshots[0])
        assert context.identity_name == "Strand"
        assert context.character_name == "艾拉"
        assert context.world_book_name == "魔法人偶"
        assert snapshots[0].content == "银港的事实。"
        archived = service.list_sessions()[0]
        assert archived.identity_name == "Strand"
        assert archived.identity_persona_name == "master"
        assert archived.character_name == "艾拉"
        assert archived.world_book_name == "魔法人偶"
    finally:
        session.close()
        engine.dispose()


def test_session_crud_and_delete_cascade_cleanup_snapshot_vectors(
    tmp_path: Path,
) -> None:
    """会话可列表、改名、读取历史并在删除后级联清理事实和向量。"""

    service, session, engine, chroma = create_service(tmp_path)
    try:
        configure_embedding(session)
        character = add_character(session)
        book, _entries = add_world_book_with_entries(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=book.id)
            )
        )
        snapshot_id = session.scalar(
            select(SessionWorldBookEntrySnapshot.id)
            .join(
                SessionContextSnapshot,
                SessionContextSnapshot.id
                == SessionWorldBookEntrySnapshot.context_snapshot_id,
            )
            .where(SessionContextSnapshot.session_id == created.id)
        )
        assert snapshot_id is not None

        updated = service.update_session(
            created.id,
            UpdateSessionPayload(title="  银港重逢  "),
        )
        assert updated.title == "银港重逢"
        assert service.list_sessions()[0].id == created.id

        service.delete_session(created.id)

        assert session.get(ChatSession, created.id) is None
        assert (
            session.scalar(
                select(func.count(SessionContextSnapshot.id)).where(
                    SessionContextSnapshot.session_id == created.id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(Message.id)).where(Message.session_id == created.id)
            )
            == 0
        )
        assert session.scalar(select(func.count(MessageBlock.id))) == 0
        assert chroma.deleted_ids == [session_entry_document_id(snapshot_id)]
    finally:
        session.close()
        engine.dispose()


def test_create_session_validates_real_character_and_world_book(tmp_path: Path) -> None:
    """真实会话创建拒绝不存在的角色卡和世界书 ID。"""

    service, session, engine, _chroma = create_service(tmp_path)
    try:
        with pytest.raises(AppError) as character_error:
            asyncio.run(
                service.create_session(
                    CreateSessionPayload(character_id="missing", world_book_id=None)
                )
            )
        assert character_error.value.message == "角色卡不存在"

        character = add_character(session)
        with pytest.raises(AppError) as book_error:
            asyncio.run(
                service.create_session(
                    CreateSessionPayload(
                        character_id=character.id,
                        world_book_id="missing",
                    )
                )
            )
        assert book_error.value.message == "世界书不存在"
    finally:
        session.close()
        engine.dispose()


def test_snapshot_embedding_failure_keeps_committed_session(tmp_path: Path) -> None:
    """快照 Embedding 失败不回滚会话，并保留明确失败状态。"""

    gateway = StubGateway(
        error=AppError(
            status_code=502,
            code="MODEL_CONNECTION_FAILED",
            message="无法连接模型服务",
        )
    )
    service, session, engine, chroma = create_service(tmp_path, gateway=gateway)
    try:
        configure_embedding(session)
        character = add_character(session)
        book, _entries = add_world_book_with_entries(session)

        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=book.id)
            )
        )

        assert session.get(ChatSession, created.id) is not None
        snapshot = session.scalar(select(SessionWorldBookEntrySnapshot))
        assert snapshot is not None
        assert snapshot.index_status == "failed"
        assert snapshot.embedding_version is None
        assert snapshot.last_index_error == "无法连接模型服务"
        assert chroma.upserts == []
    finally:
        session.close()
        engine.dispose()


def collect_basic_reply_events(
    service: ChatService,
    session_id: str,
    content: str,
) -> list[dict[str, object]]:
    """准备一轮用户消息并收集全部基础 SSE 业务事件。"""

    async def collect() -> list[dict[str, object]]:
        messages, config, api_key, retrieved_entries, user_message_id = (
            await service.prepare_chat_turn(
                session_id,
                SendMessagePayload(content=content),
            )
        )
        return [
            event
            async for event in service.stream_basic_reply(
                session_id,
                messages,
                config,
                api_key,
                retrieved_entries,
                user_message_id,
            )
        ]

    return asyncio.run(collect())


def test_basic_chat_turn_saves_user_then_atomic_assistant_blocks(
    tmp_path: Path,
) -> None:
    """基础对话先保存用户原文，再原子保存有序助手块和轮次。"""

    gateway = StubGateway(
        chat_response=(
            '{"blocks":['
            '{"type":"action","content":"她抬起头。"},'
            '{"type":"dialogue","content":"你终于来了。"}'
            "]}"
        )
    )
    service, session, engine, _chroma = create_service(tmp_path, gateway=gateway)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )

        events = collect_basic_reply_events(service, created.id, "我推开门。")

        assert [event["type"] for event in events] == [
            "block_start",
            "block_delta",
            "block_end",
            "block_start",
            "block_delta",
            "block_end",
            "done",
        ]
        assert events[0] == {
            "type": "block_start",
            "sequence": 0,
            "blockType": "action",
        }
        assert events[1] == {
            "type": "block_delta",
            "sequence": 0,
            "text": "她抬起头。",
        }
        assert events[4] == {
            "type": "block_delta",
            "sequence": 1,
            "text": "你终于来了。",
        }
        assert events[-1]["roundCount"] == 1
        messages = service.list_messages(created.id)
        assert [message.role for message in messages] == [
            "assistant",
            "user",
            "assistant",
        ]
        assert messages[1].blocks[0].content == "我推开门。"
        assert [block.type for block in messages[2].blocks] == ["action", "dialogue"]
        assert service.list_sessions()[0].round_count == 1
        stored_turn_messages = session.scalars(
            select(Message).where(
                Message.session_id == created.id,
                Message.turn_number == 1,
            )
        ).all()
        assert {message.role for message in stored_turn_messages} == {"user", "assistant"}

        request_messages = gateway.chat_requests[0]
        assert "基础角色对话规则" in request_messages[0]["content"]
        assert "姓名：用户" in request_messages[1]["content"]
        assert "名称：艾拉" in request_messages[2]["content"]
        assert request_messages[-1] == {"role": "user", "content": "我推开门。"}
    finally:
        session.close()
        engine.dispose()


def test_ninth_round_does_not_create_memory_task(tmp_path: Path) -> None:
    """尚未累计十个新轮次时只提交助手回复，不提前创建整理任务。"""

    service, session, engine, _chroma = create_service(tmp_path)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )
        chat_session = session.get(ChatSession, created.id)
        assert chat_session is not None
        chat_session.round_count = 8
        session.commit()

        events = collect_basic_reply_events(service, created.id, "第九轮。")

        assert events[-1]["roundCount"] == 9
        assert session.scalar(select(func.count(BackgroundTask.id))) == 0
    finally:
        session.close()
        engine.dispose()


def test_tenth_round_creates_one_pending_memory_task_in_reply_transaction(
    tmp_path: Path,
) -> None:
    """第十轮助手回复与目标轮次为 10 的唯一记忆任务一同提交。"""

    service, session, engine, _chroma = create_service(tmp_path)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )
        chat_session = session.get(ChatSession, created.id)
        assert chat_session is not None
        chat_session.round_count = 9
        session.commit()

        events = collect_basic_reply_events(service, created.id, "第十轮。")

        task = session.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "memory_consolidation",
                BackgroundTask.scope_id == created.id,
            )
        )
        assert events[-1]["roundCount"] == 10
        assert task is not None
        assert task.status == "pending"
        assert task.target_version == 10
        assert task.progress_current == 0
        assert task.progress_total == 10
    finally:
        session.close()
        engine.dispose()


def test_memory_task_trigger_is_idempotent_for_same_target_round(
    tmp_path: Path,
) -> None:
    """同一会话和目标轮次已有任务时，第十轮回复不会插入重复记录。"""

    service, session, engine, _chroma = create_service(tmp_path)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )
        chat_session = session.get(ChatSession, created.id)
        assert chat_session is not None
        chat_session.round_count = 9
        existing_task = BackgroundTask(
            task_type="memory_consolidation",
            status="pending",
            scope_id=created.id,
            target_version=10,
            progress_total=10,
        )
        session.add(existing_task)
        session.commit()

        collect_basic_reply_events(service, created.id, "第十轮。")

        task_count = session.scalar(
            select(func.count(BackgroundTask.id)).where(
                BackgroundTask.task_type == "memory_consolidation",
                BackgroundTask.scope_id == created.id,
                BackgroundTask.target_version == 10,
            )
        )
        assert task_count == 1
    finally:
        session.close()
        engine.dispose()


def test_ten_rounds_consolidate_and_recall_only_in_same_character_session(
    tmp_path: Path,
) -> None:
    """十轮真实对话整理后可跨会话注入同角色记忆，并隔离其他角色。"""

    gateway = StubGateway()
    chroma = RecordingChroma()
    service, session, engine, _chroma = create_service(
        tmp_path,
        gateway=gateway,
        chroma=chroma,
    )
    try:
        configure_main_model(session)
        configure_embedding(session)
        character = add_character(session)
        source_session = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )

        for round_number in range(1, 11):
            events = collect_basic_reply_events(
                service,
                source_session.id,
                f"第 {round_number} 轮对话。",
            )
            assert events[-1]["roundCount"] == round_number

        task = session.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "memory_consolidation",
                BackgroundTask.scope_id == source_session.id,
                BackgroundTask.target_version == 10,
            )
        )
        assert task is not None and task.status == "pending"
        memory_content = "用户希望与艾拉一起寻找失落的档案馆。"
        gateway.create_chat_completion = AsyncMock(
            side_effect=[
                (
                    '{"candidates":[{"type":"长期目标",'
                    f'"content":"{memory_content}",'
                    '"importance":5,"sourceRounds":[1,10]}]}'
                ),
                (f'{{"action":"create","content":"{memory_content}","importance":5}}'),
            ]
        )

        assert asyncio.run(MemoryTask(session, gateway, chroma).run(task.id)) is True
        session.expire_all()
        memory = session.scalar(
            select(LongTermMemory).where(LongTermMemory.content == memory_content)
        )
        assert memory is not None
        assert memory.status == "active"
        assert memory.index_status == "ready"
        assert memory.embedding_version == 2

        chroma.memory_result = {
            "ids": [[memory_document_id(memory.id)]],
            "distances": [[0.10]],
            "metadatas": [
                [
                    {
                        "memoryId": memory.id,
                        "characterId": character.id,
                        "status": "active",
                        "memoryType": memory.type,
                        "embeddingVersion": 2,
                        "contentHash": memory.content_hash,
                    }
                ]
            ],
        }
        same_character_session = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )
        other_character = add_character(session, name="诺亚")
        other_character_session = asyncio.run(
            service.create_session(
                CreateSessionPayload(
                    character_id=other_character.id, world_book_id=None
                )
            )
        )

        same_character_messages, *_ = asyncio.run(
            service.prepare_chat_turn(
                same_character_session.id,
                SendMessagePayload(content="我们继续寻找档案馆。"),
            )
        )
        other_character_messages, *_ = asyncio.run(
            service.prepare_chat_turn(
                other_character_session.id,
                SendMessagePayload(content="我们继续寻找档案馆。"),
            )
        )

        assert any(
            memory_content in message["content"] for message in same_character_messages
        )
        assert all(
            memory_content not in message["content"]
            for message in other_character_messages
        )
        assert len(chroma.memory_queries) == 1
    finally:
        session.close()
        engine.dispose()


def test_memory_task_failure_rolls_back_assistant_reply_and_round(
    tmp_path: Path,
) -> None:
    """任务记录无法写入时不保留半条助手回复，也不推进第十轮。"""

    service, session, engine, _chroma = create_service(tmp_path)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )
        chat_session = session.get(ChatSession, created.id)
        assert chat_session is not None
        chat_session.round_count = 9
        session.commit()
        original_add = service.repository.add

        def reject_memory_task(record):
            """模拟任务插入失败，同时允许消息和内容块先进入当前事务。"""

            if isinstance(record, BackgroundTask):
                raise TypeError("task insert failed")
            return original_add(record)

        with patch.object(service.repository, "add", side_effect=reject_memory_task):
            events = collect_basic_reply_events(service, created.id, "第十轮。")

        session.expire_all()
        refreshed = session.get(ChatSession, created.id)
        assert refreshed is not None
        assert [event["type"] for event in events] == [
            "block_start",
            "block_delta",
            "block_end",
            "error",
        ]
        assert events[-1]["message"] == "角色回复生成失败"
        assert refreshed.round_count == 9
        assert session.scalar(select(func.count(BackgroundTask.id))) == 0
        roles = session.scalars(
            select(Message.role)
            .where(Message.session_id == created.id)
            .order_by(Message.position)
        ).all()
        assert roles == ["assistant", "user"]
    finally:
        session.close()
        engine.dispose()


def test_chat_turn_injects_dual_rag_and_persists_worldbook_retrieval(
    tmp_path: Path,
) -> None:
    """单次查询向量供双 Retriever 共用，并按固定顺序注入和保存命中。"""

    gateway = StubGateway()
    chroma = RecordingChroma()
    service, session, engine, _chroma = create_service(
        tmp_path,
        gateway=gateway,
        chroma=chroma,
    )
    try:
        configure_main_model(session)
        configure_embedding(session)
        character = add_character(session)
        book, _entries = add_world_book_with_entries(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=book.id)
            )
        )
        snapshot = session.scalar(select(SessionWorldBookEntrySnapshot))
        assert snapshot is not None
        memory_content = "用户喜欢使用胶片相机。"
        memory = LongTermMemory(
            character_id=character.id,
            type="用户偏好",
            content=memory_content,
            importance=4,
            status="active",
            source_label="测试对话",
            memory_version=1,
            index_status="ready",
            embedding_version=2,
            content_hash=calculate_content_hash(memory_content),
        )
        session.add(memory)
        session.commit()

        worldbook_metadata = chroma.upserts[0]["metadatas"][0]
        chroma.worldbook_result = {
            "ids": [[session_entry_document_id(snapshot.id)]],
            "distances": [[0.10]],
            "metadatas": [[worldbook_metadata]],
        }
        chroma.memory_result = {
            "ids": [[memory_document_id(memory.id)]],
            "distances": [[0.10]],
            "metadatas": [
                [
                    {
                        "memoryId": memory.id,
                        "characterId": character.id,
                        "status": "active",
                        "memoryType": memory.type,
                        "embeddingVersion": 2,
                        "contentHash": memory.content_hash,
                    }
                ]
            ],
        }

        events = collect_basic_reply_events(service, created.id, "带我去银港拍照。")

        assert events[0] == {"type": "retrieval", "entries": ["银港"]}
        assert events[-1]["type"] == "done"
        assert len(gateway.embedding_requests) == 2
        assert gateway.embedding_requests[-1]["texts"] == [
            "最近 4 个完整对话轮次：\n无\n当前输入：\n带我去银港拍照。"
        ]
        assert len(chroma.worldbook_queries) == 1
        assert len(chroma.memory_queries) == 1

        request_messages = gateway.chat_requests[-1]
        assert "基础角色对话规则" in request_messages[0]["content"]
        assert "用户身份快照" in request_messages[1]["content"]
        assert "角色卡快照" in request_messages[2]["content"]
        assert "世界书检索结果" in request_messages[3]["content"]
        assert "不得覆盖或修改角色卡" in request_messages[3]["content"]
        assert "银港的事实" in request_messages[3]["content"]
        assert "长期记忆检索结果" in request_messages[4]["content"]
        assert "不得修改角色规则" in request_messages[4]["content"]
        assert memory_content in request_messages[4]["content"]
        assert request_messages[-1] == {"role": "user", "content": "带我去银港拍照。"}

        messages = service.list_messages(created.id)
        assert messages[-1].role == "assistant"
        assert messages[-1].retrieved == ["银港"]
    finally:
        session.close()
        engine.dispose()


def test_embedding_failure_degrades_to_non_vector_worldbook_retrieval(
    tmp_path: Path,
) -> None:
    """Embedding 服务失败时仍使用常驻和关键词条目完成对话。"""

    gateway = StubGateway()
    chroma = RecordingChroma()
    service, session, engine, _chroma = create_service(
        tmp_path,
        gateway=gateway,
        chroma=chroma,
    )
    try:
        configure_main_model(session)
        configure_embedding(session)
        character = add_character(session)
        book, _entries = add_world_book_with_entries(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=book.id)
            )
        )
        gateway.error = AppError(
            status_code=502,
            code="MODEL_CONNECTION_FAILED",
            message="无法连接模型服务",
        )
        with patch("app.services.chat_service.logger.warning") as warning:
            events = collect_basic_reply_events(service, created.id, "银港现在安全吗？")

        assert events[0] == {"type": "retrieval", "entries": ["银港"]}
        assert events[-1]["type"] == "done"
        assert chroma.worldbook_queries == []
        assert any(
            "Embedding 服务失败" in call.args[0] for call in warning.call_args_list
        )
        assert service.list_messages(created.id)[-1].retrieved == ["银港"]
    finally:
        session.close()
        engine.dispose()


def test_unconfigured_embedding_degrades_to_non_vector_worldbook_retrieval(
    tmp_path: Path,
) -> None:
    """Embedding 未配置时跳过外部调用，并保留常驻和关键词世界书结果。"""

    gateway = StubGateway()
    chroma = RecordingChroma()
    service, session, engine, _chroma = create_service(
        tmp_path,
        gateway=gateway,
        chroma=chroma,
    )
    try:
        configure_main_model(session)
        configure_embedding(session)
        character = add_character(session)
        book, _entries = add_world_book_with_entries(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=book.id)
            )
        )
        initial_embedding_request_count = len(gateway.embedding_requests)
        embedding_config = session.get(ModelConfig, "embed")
        assert embedding_config is not None
        embedding_config.base_url = ""
        embedding_config.model_name = ""
        embedding_config.secret_ref = None
        embedding_config.vector_dimension = None
        session.commit()

        with patch("app.services.chat_service.logger.warning") as warning:
            events = collect_basic_reply_events(service, created.id, "银港现在安全吗？")

        assert events[0] == {"type": "retrieval", "entries": ["银港"]}
        assert events[-1]["type"] == "done"
        assert len(gateway.embedding_requests) == initial_embedding_request_count
        assert chroma.worldbook_queries == []
        assert chroma.memory_queries == []
        assert any(
            "Embedding 模型未配置" in call.args[0] for call in warning.call_args_list
        )
    finally:
        session.close()
        engine.dispose()


def test_chroma_failure_degrades_to_non_vector_worldbook_retrieval(
    tmp_path: Path,
) -> None:
    """Chroma 查询失败时保留常驻条目并记录 WARNING。"""

    gateway = StubGateway()
    chroma = RecordingChroma()
    service, session, engine, _chroma = create_service(
        tmp_path,
        gateway=gateway,
        chroma=chroma,
    )
    try:
        configure_main_model(session)
        configure_embedding(session)
        character = add_character(session)
        book, _entries = add_world_book_with_entries(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=book.id)
            )
        )
        chroma.worldbook_error = RuntimeError("Chroma 暂不可用")
        with patch("app.services.chat_service.logger.warning") as warning:
            events = collect_basic_reply_events(service, created.id, "继续刚才的故事。")

        assert events[0] == {"type": "retrieval", "entries": ["银港"]}
        assert events[-1]["type"] == "done"
        assert len(chroma.worldbook_queries) == 1
        assert any(
            "世界书向量检索失败" in call.args[0] for call in warning.call_args_list
        )
    finally:
        session.close()
        engine.dispose()


def test_empty_dual_rag_continues_without_retrieval_event_or_prompt_context(
    tmp_path: Path,
) -> None:
    """绑定世界书但双 RAG 均为空时记录 WARNING，且继续生成无检索事件的回复。"""

    gateway = StubGateway()
    chroma = RecordingChroma()
    service, session, engine, _chroma = create_service(
        tmp_path,
        gateway=gateway,
        chroma=chroma,
    )
    try:
        configure_main_model(session)
        configure_embedding(session)
        character = add_character(session)
        book, _entries = add_world_book_with_entries(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=book.id)
            )
        )
        snapshot = session.scalar(select(SessionWorldBookEntrySnapshot))
        assert snapshot is not None
        snapshot.resident = 0
        session.commit()

        with patch("app.services.chat_service.logger.warning") as warning:
            events = collect_basic_reply_events(service, created.id, "继续刚才的故事。")

        assert events[0]["type"] == "block_start"
        assert all(event["type"] != "retrieval" for event in events)
        assert events[-1]["type"] == "done"
        assert len(chroma.worldbook_queries) == 1
        assert chroma.memory_queries == []
        assert any("世界书检索为空" in call.args[0] for call in warning.call_args_list)
        assert any(
            "长期记忆检索为空" in call.args[0] for call in warning.call_args_list
        )
        assert not any(
            message["content"].startswith(("世界书检索结果：", "长期记忆检索结果："))
            for message in gateway.chat_requests[-1][1:]
        )
    finally:
        session.close()
        engine.dispose()


def test_memory_chroma_failure_degrades_to_empty_context(
    tmp_path: Path,
) -> None:
    """长期记忆 Chroma 查询失败时不注入记忆且继续完成对话。"""

    gateway = StubGateway()
    chroma = RecordingChroma()
    service, session, engine, _chroma = create_service(
        tmp_path,
        gateway=gateway,
        chroma=chroma,
    )
    try:
        configure_main_model(session)
        configure_embedding(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )
        memory_content = "用户曾与角色约定再次见面。"
        session.add(
            LongTermMemory(
                character_id=character.id,
                type="角色承诺",
                content=memory_content,
                importance=4,
                status="active",
                source_label="测试对话",
                memory_version=1,
                index_status="ready",
                embedding_version=2,
                content_hash=calculate_content_hash(memory_content),
            )
        )
        session.commit()
        chroma.memory_error = RuntimeError("Chroma 暂不可用")

        with patch("app.services.chat_service.logger.warning") as warning:
            events = collect_basic_reply_events(
                service, created.id, "我们是不是有过约定？"
            )

        assert events[0]["type"] == "block_start"
        assert events[-1]["type"] == "done"
        assert len(chroma.memory_queries) == 1
        assert any(
            "长期记忆检索失败" in call.args[0] for call in warning.call_args_list
        )
        assert not any(
            message["content"].startswith("长期记忆检索结果：")
            for message in gateway.chat_requests[-1][1:]
        )
    finally:
        session.close()
        engine.dispose()


def test_basic_chat_limits_history_to_latest_twenty_rounds(tmp_path: Path) -> None:
    """数据库保留完整历史，但第 21 轮模型上下文只携带最近 40 条对话消息。"""

    gateway = StubGateway()
    service, session, engine, _chroma = create_service(tmp_path, gateway=gateway)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )

        for round_number in range(1, 22):
            collect_basic_reply_events(service, created.id, f"第{round_number}轮")

        final_request = gateway.chat_requests[-1]
        assert len(final_request) == 44
        assert final_request[3] == {"role": "user", "content": "第1轮"}
        assert final_request[-1] == {"role": "user", "content": "第21轮"}
        assert len(service.list_messages(created.id)) == 43
        assert service.list_sessions()[0].round_count == 21
    finally:
        session.close()
        engine.dispose()


def test_retrieval_text_uses_latest_four_complete_rounds(tmp_path: Path) -> None:
    """检索文本忽略开场白和孤立用户消息，只保留最近四个完整轮次。"""

    gateway = StubGateway()
    service, session, engine, _chroma = create_service(tmp_path, gateway=gateway)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )
        for round_number in range(1, 7):
            collect_basic_reply_events(service, created.id, f"第{round_number}轮")

        gateway.chat_error = AppError(
            status_code=502,
            code="MODEL_CONNECTION_FAILED",
            message="无法连接模型服务",
        )
        collect_basic_reply_events(service, created.id, "失败后保留的孤立输入")

        retrieval_text = service.build_retrieval_text(
            session_id=created.id,
            current_user_message_id="尚未保存的当前消息",
            current_input="当前问题",
        )

        assert retrieval_text == (
            "最近 4 个完整对话轮次：\n"
            "用户：第3轮\n"
            "角色：[台词] 收到。\n"
            "用户：第4轮\n"
            "角色：[台词] 收到。\n"
            "用户：第5轮\n"
            "角色：[台词] 收到。\n"
            "用户：第6轮\n"
            "角色：[台词] 收到。\n"
            "当前输入：\n"
            "当前问题"
        )
        assert "你终于来了" not in retrieval_text
        assert "失败后保留的孤立输入" not in retrieval_text
    finally:
        session.close()
        engine.dispose()


def test_model_failure_keeps_user_message_without_partial_assistant(
    tmp_path: Path,
) -> None:
    """流建立后的模型错误以 error 结束，用户原文保留且轮次不增加。"""

    gateway = StubGateway(
        chat_error=AppError(
            status_code=502,
            code="MODEL_CONNECTION_FAILED",
            message="无法连接模型服务",
        )
    )
    service, session, engine, _chroma = create_service(tmp_path, gateway=gateway)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )

        events = collect_basic_reply_events(service, created.id, "请回应我。")

        assert events == [{"type": "error", "message": "无法连接模型服务"}]
        messages = service.list_messages(created.id)
        assert [message.role for message in messages] == ["assistant", "user"]
        assert messages[-1].blocks[0].content == "请回应我。"
        assert service.list_sessions()[0].round_count == 0
    finally:
        session.close()
        engine.dispose()


def test_late_invalid_block_discards_preview_and_does_not_save_assistant(
    tmp_path: Path,
) -> None:
    """已有完整块被展示后协议才损坏时，仍不保存部分助手消息。"""

    gateway = StubGateway(
        chat_response=(
            '{"blocks":['
            '{"type":"action","content":"她抬起头。"},'
            "invalid]}"
        )
    )
    service, session, engine, _chroma = create_service(tmp_path, gateway=gateway)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )

        events = collect_basic_reply_events(service, created.id, "请回应我。")

        assert [event["type"] for event in events] == [
            "block_start",
            "block_delta",
            "block_end",
            "error",
        ]
        assert events[-1]["message"] == "主模型返回的内容块结构无效"
        assert [message.role for message in service.list_messages(created.id)] == [
            "assistant",
            "user",
        ]
        assert service.list_sessions()[0].round_count == 0
    finally:
        session.close()
        engine.dispose()


def test_unconfigured_main_model_rejects_before_saving_user_message(
    tmp_path: Path,
) -> None:
    """主模型未配置时返回普通业务错误，不写入待发送用户消息。"""

    service, session, engine, _chroma = create_service(tmp_path)
    try:
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )

        with pytest.raises(AppError) as error:
            asyncio.run(
                service.prepare_chat_turn(
                    created.id,
                    SendMessagePayload(content="不会被保存"),
                )
            )

        assert error.value.code == "MODEL_NOT_CONFIGURED"
        assert [message.role for message in service.list_messages(created.id)] == [
            "assistant"
        ]
    finally:
        session.close()
        engine.dispose()


def test_last_reply_can_regenerate_continue_and_delete_in_place(tmp_path: Path) -> None:
    """最后一条助手回复可原位覆盖、追加，并最终物理删除整轮。"""

    gateway = StubGateway()
    service, session, engine, _chroma = create_service(tmp_path, gateway=gateway)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )
        collect_basic_reply_events(service, created.id, "我们出发吧。")
        assistant_id = service.list_messages(created.id)[-1].id

        async def run_action(action: str) -> list[dict[str, object]]:
            messages, config, api_key, retrieved, signature = (
                await service.prepare_message_action(created.id, assistant_id, action)
            )
            return [
                event
                async for event in service.stream_message_action(
                    created.id,
                    assistant_id,
                    action,
                    messages,
                    config,
                    api_key,
                    retrieved,
                    signature,
                )
            ]

        gateway.chat_response = (
            '{"blocks":['
            '{"type":"action","content":"她推开门。"},'
            '{"type":"dialogue","content":"走吧。"}'
            "]}"
        )
        regenerate_events = asyncio.run(run_action("regenerate"))
        regenerated = service.list_messages(created.id)[-1]
        assert [event["type"] for event in regenerate_events] == [
            "block_start",
            "block_delta",
            "block_end",
            "block_start",
            "block_delta",
            "block_end",
            "done",
        ]
        assert regenerate_events[-1]["messageId"] == assistant_id
        assert regenerated.id == assistant_id
        assert [block.content for block in regenerated.blocks] == ["她推开门。", "走吧。"]
        assert service.list_sessions()[0].round_count == 1

        gateway.chat_response = '{"blocks":[{"type":"dialogue","content":"别落下。"}]}'
        continue_events = asyncio.run(run_action("continue"))
        continued = service.list_messages(created.id)[-1]
        assert [event["type"] for event in continue_events] == [
            "block_start",
            "block_delta",
            "block_end",
            "done",
        ]
        assert continue_events[-1]["messageId"] == assistant_id
        assert [block.sequence for block in continued.blocks] == [0, 1, 2]
        assert continued.blocks[-1].content == "别落下。"
        assert gateway.chat_requests[-1][-1]["content"] == (
            "继续上一条角色回复，从结束处自然续写。"
            "不要重复已有内容，也不要假设用户产生了新的动作或台词。"
        )

        service.delete_turn(created.id, assistant_id)
        assert [message.role for message in service.list_messages(created.id)] == ["assistant"]
        assert service.list_sessions()[0].round_count == 0
        assert session.scalar(
            select(func.count(Message.id)).where(Message.id == assistant_id)
        ) == 0
    finally:
        session.close()
        engine.dispose()


def test_deleting_consolidated_last_turn_keeps_progress_and_next_number(tmp_path: Path) -> None:
    """已整理最后一轮物理删除后保留进度，下一轮使用新的稳定编号。"""

    service, session, engine, _chroma = create_service(tmp_path)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )
        collect_basic_reply_events(service, created.id, "第十轮内容")
        turn_messages = session.scalars(
            select(Message).where(Message.turn_number == 1)
        ).all()
        for message in turn_messages:
            message.turn_number = 10
        chat_session = session.get(ChatSession, created.id)
        assert chat_session is not None
        chat_session.round_count = 10
        chat_session.consolidated_round = 10
        session.commit()
        assistant_id = next(message.id for message in turn_messages if message.role == "assistant")

        service.delete_turn(created.id, assistant_id)
        session.refresh(chat_session)
        assert chat_session.round_count == 10
        assert chat_session.consolidated_round == 10

        collect_basic_reply_events(service, created.id, "新的后续内容")
        new_turn_numbers = session.scalars(
            select(Message.turn_number)
            .where(Message.session_id == created.id, Message.turn_number.is_not(None))
            .distinct()
        ).all()
        assert new_turn_numbers == [11]
    finally:
        session.close()
        engine.dispose()


def test_recommended_reply_uses_history_without_writing_message(tmp_path: Path) -> None:
    """推荐回复读取当前会话，但只返回草稿内容且不写入数据库。"""

    gateway = StubGateway(chat_response='{"content":"我们去看看吧。"}')
    service, session, engine, _chroma = create_service(tmp_path, gateway=gateway)
    try:
        configure_main_model(session)
        character = add_character(session)
        created = asyncio.run(
            service.create_session(
                CreateSessionPayload(character_id=character.id, world_book_id=None)
            )
        )
        before_count = session.scalar(select(func.count(Message.id)))

        result = asyncio.run(service.generate_recommended_reply(created.id))

        assert result.content == "我们去看看吧。"
        assert session.scalar(select(func.count(Message.id))) == before_count
        assert "用户推荐回复规则" in gateway.chat_requests[-1][0]["content"]
        assert gateway.chat_requests[-1][-1]["content"] == "[台词] 你终于来了。"
    finally:
        session.close()
        engine.dispose()
