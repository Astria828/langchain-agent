"""阶段 7.3–7.9 长期记忆读取、整合、持久化与任务测试。"""

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
    MemorySource,
    Message,
    MessageBlock,
    ModelConfig,
    SessionContextSnapshot,
)
from app.main import create_app
from app.repositories.chroma_repository import (
    calculate_content_hash,
    memory_document_id,
)
from app.schemas.dto import (
    MemoryCandidate,
    MemoryConsolidationDecision,
)
from app.services.memory_service import MemoryConsolidationPlan, MemoryService
from app.tasks.memory_task import (
    MemoryTask,
    recover_interrupted_memory_tasks,
    retry_failed_memory_task,
    run_pending_memory_tasks,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from scripts.init_db import initialize_database


class StubGateway:
    """按顺序返回结构化文本，并记录候选 Embedding 请求。"""

    def __init__(self, chat_responses: list[str]) -> None:
        self.chat_responses = list(chat_responses)
        self.chat_requests: list[list[dict[str, str]]] = []
        self.embedding_requests: list[dict[str, object]] = []
        self.embedding_error_on_call: int | None = None

    async def create_chat_completion(self, **kwargs) -> str:
        """返回下一条提取或整合响应。"""

        self.chat_requests.append(kwargs["messages"])
        return self.chat_responses.pop(0)

    async def create_embeddings(self, **kwargs) -> list[list[float]]:
        """为每条候选返回固定三维向量。"""

        self.embedding_requests.append(kwargs)
        if self.embedding_error_on_call == len(self.embedding_requests):
            raise RuntimeError("embedding failed")
        return [[0.1, 0.2, 0.3] for _text in kwargs["texts"]]


class RecordingChroma:
    """记录同角色记忆检索，并返回受控相似结果。"""

    def __init__(self) -> None:
        self.result: dict[str, object] = {
            "ids": [[]],
            "distances": [[]],
            "metadatas": [[]],
        }
        self.queries: list[dict[str, object]] = []
        self.upserts: list[dict[str, object]] = []
        self.deleted_ids: list[str] = []
        self.upsert_error: Exception | None = None
        self.delete_error: Exception | None = None

    def query_memories(self, **kwargs) -> dict[str, object]:
        """记录查询边界并返回当前结果。"""

        self.queries.append(kwargs)
        return self.result

    def upsert_memories(self, **kwargs) -> None:
        """记录长期记忆向量写入或抛出受控故障。"""

        if self.upsert_error is not None:
            raise self.upsert_error
        self.upserts.append(kwargs)

    def delete_memories(self, *, ids: list[str]) -> None:
        """记录长期记忆向量删除或抛出受控故障。"""

        self.deleted_ids.extend(ids)
        if self.delete_error is not None:
            raise self.delete_error


def create_database(tmp_path: Path) -> tuple[Session, object]:
    """创建隔离的阶段 7 服务测试数据库。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    return create_session_factory(engine)(), engine


def configure_models(session: Session, *, embedding: bool = True) -> None:
    """配置可用主模型，并按需配置当前三维 Embedding。"""

    main = session.get(ModelConfig, "main")
    assert main is not None
    main.base_url = "https://models.example/v1"
    main.model_name = "chat-model"
    main.secret_ref = protect_api_key("sk-main-test")
    if embedding:
        embed = session.get(ModelConfig, "embed")
        assert embed is not None
        embed.base_url = "https://models.example/v1"
        embed.model_name = "embed-model"
        embed.secret_ref = protect_api_key("sk-embed-test")
        embed.vector_dimension = 3
        embed.config_version = 2
        embed.rebuild_required = 0
    session.commit()


def add_message(
    session: Session,
    *,
    session_id: str,
    position: int,
    role: str,
    blocks: list[tuple[str, str]],
) -> Message:
    """添加一条带连续内容块的测试消息。"""

    message = Message(session_id=session_id, position=position, role=role)
    session.add(message)
    session.flush()
    for sequence, (block_type, content) in enumerate(blocks):
        session.add(
            MessageBlock(
                message_id=message.id,
                sequence=sequence,
                type=block_type,
                content=content,
            )
        )
    return message


def add_task_context(
    session: Session,
    *,
    round_count: int,
    consolidated_round: int,
    target_round: int,
) -> tuple[Character, ChatSession, BackgroundTask, dict[int, tuple[str, str]]]:
    """创建会话快照、完整轮次、干扰消息和记忆任务。"""

    character = Character(name="艾拉")
    session.add(character)
    session.flush()
    chat_session = ChatSession(
        title="测试会话",
        character_id=character.id,
        round_count=round_count,
        consolidated_round=consolidated_round,
    )
    session.add(chat_session)
    session.flush()
    session.add(
        SessionContextSnapshot(
            session_id=chat_session.id,
            identity_source_id="identity-current",
            identity_name="用户",
            identity_persona_name="旅人",
            identity_bio="",
            character_source_id=character.id,
            character_name=character.name,
            character_introduction="",
            character_system_prompt="",
            character_dialogue_examples_json="[]",
        )
    )

    position = 0
    add_message(
        session,
        session_id=chat_session.id,
        position=position,
        role="assistant",
        blocks=[("dialogue", "开场白")],
    )
    position += 1
    source_ids: dict[int, tuple[str, str]] = {}
    for absolute_round in range(1, round_count + 1):
        user = add_message(
            session,
            session_id=chat_session.id,
            position=position,
            role="user",
            blocks=[("dialogue", f"用户第 {absolute_round} 轮")],
        )
        position += 1
        assistant = add_message(
            session,
            session_id=chat_session.id,
            position=position,
            role="assistant",
            blocks=[
                ("action", f"第 {absolute_round} 轮动作"),
                ("dialogue", f"角色第 {absolute_round} 轮台词"),
            ],
        )
        position += 1
        source_ids[absolute_round] = (user.id, assistant.id)
        if absolute_round == 5:
            add_message(
                session,
                session_id=chat_session.id,
                position=position,
                role="memo",
                blocks=[("dialogue", "整理提示")],
            )
            position += 1

    add_message(
        session,
        session_id=chat_session.id,
        position=position,
        role="user",
        blocks=[("dialogue", "尚未完成的孤立用户消息")],
    )
    task = BackgroundTask(
        task_type="memory_consolidation",
        status="pending",
        scope_id=chat_session.id,
        target_version=target_round,
        progress_total=10,
    )
    session.add(task)
    session.commit()
    return character, chat_session, task, source_ids


def add_memory(
    session: Session,
    character: Character,
    *,
    content: str,
    memory_type: str,
) -> LongTermMemory:
    """添加当前版本可检索的有效长期记忆。"""

    memory = LongTermMemory(
        character_id=character.id,
        type=memory_type,
        content=content,
        importance=3,
        status="active",
        source_label="旧会话",
        memory_version=1,
        index_status="ready",
        embedding_version=2,
        content_hash=calculate_content_hash(content),
    )
    session.add(memory)
    session.commit()
    return memory


def memory_metadata(memory: LongTermMemory) -> dict[str, object]:
    """构造通过 Retriever SQLite 回查的标准 metadata。"""

    return {
        "memoryId": memory.id,
        "characterId": memory.character_id,
        "status": "active",
        "memoryType": memory.type,
        "embeddingVersion": memory.embedding_version,
        "contentHash": memory.content_hash,
    }


def test_load_target_rounds_selects_exact_interval_and_preserves_sources(
    tmp_path: Path,
) -> None:
    """目标 20 只读取绝对轮次 11–20，并忽略开场白、memo 和孤立用户消息。"""

    session, engine = create_database(tmp_path)
    try:
        _character, _chat_session, task, source_ids = add_task_context(
            session,
            round_count=20,
            consolidated_round=10,
            target_round=20,
        )
        service = MemoryService(session, StubGateway([]), RecordingChroma())

        rounds = service.load_target_rounds(task.id)
        extraction_input = service.build_extraction_input(rounds)

        assert len(rounds) == 10
        assert (rounds[0].batch_round, rounds[0].absolute_round) == (1, 11)
        assert (rounds[-1].batch_round, rounds[-1].absolute_round) == (10, 20)
        assert (rounds[0].user_message_id, rounds[0].assistant_message_id) == source_ids[11]
        assert "用户：用户第 11 轮" in extraction_input
        assert "[动作] 第 20 轮动作\n[台词] 角色第 20 轮台词" in extraction_input
        assert "开场白" not in extraction_input
        assert "整理提示" not in extraction_input
        assert "孤立用户消息" not in extraction_input
    finally:
        session.close()
        engine.dispose()


def test_load_target_rounds_rejects_non_contiguous_target(tmp_path: Path) -> None:
    """目标轮次不是 consolidated_round 后的下一个十轮区间时拒绝执行。"""

    session, engine = create_database(tmp_path)
    try:
        _character, _chat_session, task, _source_ids = add_task_context(
            session,
            round_count=20,
            consolidated_round=0,
            target_round=20,
        )
        service = MemoryService(session, StubGateway([]), RecordingChroma())

        with pytest.raises(AppError) as error:
            service.load_target_rounds(task.id)

        assert error.value.code == "MEMORY_TASK_INVALID"
    finally:
        session.close()
        engine.dispose()


def test_load_target_rounds_rejects_character_snapshot_mismatch(tmp_path: Path) -> None:
    """会话当前角色与创建时角色快照不一致时不得整理。"""

    session, engine = create_database(tmp_path)
    try:
        _character, chat_session, task, _source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        other_character = Character(name="诺亚")
        session.add(other_character)
        session.flush()
        snapshot = session.scalar(
            select(SessionContextSnapshot).where(
                SessionContextSnapshot.session_id == chat_session.id
            )
        )
        assert snapshot is not None
        snapshot.character_source_id = other_character.id
        session.commit()
        service = MemoryService(session, StubGateway([]), RecordingChroma())

        with pytest.raises(AppError) as error:
            service.load_target_rounds(task.id)

        assert error.value.code == "MEMORY_TASK_INVALID"
        assert "角色绑定" in error.value.message
    finally:
        session.close()
        engine.dispose()


def test_load_target_rounds_rejects_missing_complete_round(tmp_path: Path) -> None:
    """round_count 与真实完整消息轮次不一致时不得拼出不完整批次。"""

    session, engine = create_database(tmp_path)
    try:
        _character, chat_session, task, _source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        last_assistant = session.scalar(
            select(Message)
            .where(
                Message.session_id == chat_session.id,
                Message.role == "assistant",
            )
            .order_by(Message.position.desc())
        )
        assert last_assistant is not None
        session.delete(last_assistant)
        session.commit()
        service = MemoryService(session, StubGateway([]), RecordingChroma())

        with pytest.raises(AppError) as error:
            service.load_target_rounds(task.id)

        assert error.value.code == "MEMORY_TASK_INVALID"
        assert "完整消息轮次" in error.value.message
    finally:
        session.close()
        engine.dispose()


def test_exact_duplicate_skips_embedding_and_consolidation_chain(tmp_path: Path) -> None:
    """当前角色同类型同正文记忆直接去重，并保留候选来源消息。"""

    session, engine = create_database(tmp_path)
    try:
        character, _chat_session, task, source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        configure_models(session, embedding=False)
        content = "用户喜欢在雨天散步。"
        existing = add_memory(session, character, content=content, memory_type="用户偏好")
        gateway = StubGateway(
            [
                (
                    '{"candidates":[{"type":"用户偏好",'
                    '"content":"用户喜欢在雨天散步。",'
                    '"importance":3,"sourceRounds":[2,4]}]}'
                )
            ]
        )
        chroma = RecordingChroma()
        service = MemoryService(session, gateway, chroma)

        plans = asyncio.run(service.extract_and_consolidate(task.id))

        assert len(plans) == 1
        assert plans[0].decision.action == "deduplicate"
        assert plans[0].decision.target_memory_id == existing.id
        assert plans[0].source_message_ids == (*source_ids[2], *source_ids[4])
        assert gateway.embedding_requests == []
        assert len(gateway.chat_requests) == 1
        assert chroma.queries == []
    finally:
        session.close()
        engine.dispose()


def test_non_duplicate_uses_current_role_similarity_and_merge_decision(tmp_path: Path) -> None:
    """非重复候选只把当前角色当前版本的相似记忆交给整合 Chain。"""

    session, engine = create_database(tmp_path)
    try:
        character, _chat_session, task, source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        configure_models(session)
        existing = add_memory(
            session,
            character,
            content="艾拉答应帮助用户寻找档案馆。",
            memory_type="角色承诺",
        )
        other_character = Character(name="诺亚")
        session.add(other_character)
        session.commit()
        foreign = add_memory(
            session,
            other_character,
            content="诺亚答应提供地图。",
            memory_type="角色承诺",
        )
        gateway = StubGateway(
            [
                (
                    '{"candidates":[{"type":"角色承诺",'
                    '"content":"艾拉承诺陪用户寻找旧档案馆。",'
                    '"importance":4,"sourceRounds":[3]}]}'
                ),
                (
                    '{"action":"merge","targetMemoryId":"'
                    + existing.id
                    + '","content":"艾拉承诺陪用户寻找旧档案馆。","importance":4}'
                ),
            ]
        )
        chroma = RecordingChroma()
        chroma.result = {
            "ids": [[memory_document_id(existing.id)]],
            "distances": [[0.1]],
            "metadatas": [[memory_metadata(existing)]],
        }
        service = MemoryService(session, gateway, chroma)

        plans = asyncio.run(service.extract_and_consolidate(task.id))

        assert plans[0].decision.action == "merge"
        assert plans[0].decision.target_memory_id == existing.id
        assert plans[0].source_message_ids == source_ids[3]
        assert gateway.embedding_requests[0]["texts"] == ["艾拉承诺陪用户寻找旧档案馆。"]
        consolidation_payload = json.loads(gateway.chat_requests[1][1]["content"])
        assert [item["id"] for item in consolidation_payload["existingMemories"]] == [existing.id]
        assert foreign.id not in gateway.chat_requests[1][1]["content"]
        assert chroma.queries[0]["where"] == {
            "$and": [
                {"characterId": character.id},
                {"status": "active"},
                {"embeddingVersion": 2},
            ]
        }
    finally:
        session.close()
        engine.dispose()


def test_exact_content_from_other_character_does_not_deduplicate(tmp_path: Path) -> None:
    """其他角色的同类型同正文记忆不能成为当前角色的精确去重目标。"""

    session, engine = create_database(tmp_path)
    try:
        _character, _chat_session, task, _source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        configure_models(session)
        other_character = Character(name="诺亚")
        session.add(other_character)
        session.commit()
        content = "用户喜欢收集旧唱片。"
        foreign = add_memory(
            session,
            other_character,
            content=content,
            memory_type="用户偏好",
        )
        gateway = StubGateway(
            [
                (
                    '{"candidates":[{"type":"用户偏好",'
                    '"content":"用户喜欢收集旧唱片。",'
                    '"importance":3,"sourceRounds":[6]}]}'
                ),
                '{"action":"create","content":"用户喜欢收集旧唱片。","importance":3}',
            ]
        )
        service = MemoryService(session, gateway, RecordingChroma())

        plans = asyncio.run(service.extract_and_consolidate(task.id))

        assert plans[0].decision.action == "create"
        assert foreign.id not in gateway.chat_requests[1][1]["content"]
        assert len(gateway.embedding_requests) == 1
    finally:
        session.close()
        engine.dispose()


def test_conflicting_actions_for_same_memory_fail_whole_plan(tmp_path: Path) -> None:
    """同批次两条候选不能同时修改同一已有记忆。"""

    session, engine = create_database(tmp_path)
    try:
        character, _chat_session, task, _source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        configure_models(session)
        existing = add_memory(
            session,
            character,
            content="艾拉答应帮助用户寻找档案馆。",
            memory_type="角色承诺",
        )
        gateway = StubGateway(
            [
                (
                    '{"candidates":['
                    '{"type":"角色承诺","content":"艾拉承诺陪用户去档案馆。",'
                    '"importance":4,"sourceRounds":[2]},'
                    '{"type":"角色承诺","content":"艾拉承诺带上档案馆钥匙。",'
                    '"importance":4,"sourceRounds":[7]}]}'
                ),
                (
                    '{"action":"merge","targetMemoryId":"'
                    + existing.id
                    + '","content":"第一次合并。","importance":4}'
                ),
                (
                    '{"action":"update","targetMemoryId":"'
                    + existing.id
                    + '","content":"第二次更新。","importance":4}'
                ),
            ]
        )
        chroma = RecordingChroma()
        chroma.result = {
            "ids": [[memory_document_id(existing.id)]],
            "distances": [[0.1]],
            "metadatas": [[memory_metadata(existing)]],
        }
        service = MemoryService(session, gateway, chroma)

        with pytest.raises(AppError) as error:
            asyncio.run(service.extract_and_consolidate(task.id))

        assert error.value.code == "MODEL_OUTPUT_INVALID"
        assert "冲突动作" in error.value.message
        assert len(chroma.queries) == 2
    finally:
        session.close()
        engine.dispose()


def test_empty_extraction_does_not_call_embedding_or_consolidation(tmp_path: Path) -> None:
    """空候选是合法只读结果，不产生无意义的相似检索或整合调用。"""

    session, engine = create_database(tmp_path)
    try:
        _character, _chat_session, task, _source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        configure_models(session, embedding=False)
        gateway = StubGateway(['{"candidates":[]}'])
        chroma = RecordingChroma()
        service = MemoryService(session, gateway, chroma)

        assert asyncio.run(service.extract_and_consolidate(task.id)) == []
        assert len(gateway.chat_requests) == 1
        assert gateway.embedding_requests == []
        assert chroma.queries == []
    finally:
        session.close()
        engine.dispose()


def test_persist_and_sync_all_memory_actions_atomically(tmp_path: Path) -> None:
    """五种非忽略动作写入来源，必要向量成功后才推进整理进度。"""

    session, engine = create_database(tmp_path)
    try:
        character, chat_session, task, source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        configure_models(session)
        duplicate = add_memory(
            session,
            character,
            content="用户喜欢雨天。",
            memory_type="用户偏好",
        )
        updated = add_memory(
            session,
            character,
            content="艾拉答应提供线索。",
            memory_type="角色承诺",
        )
        invalidated = add_memory(
            session,
            character,
            content="双方仍互不信任。",
            memory_type="关系变化",
        )
        plans = [
            MemoryConsolidationPlan(
                candidate=MemoryCandidate(
                    type="用户偏好",
                    content="用户喜欢雨天。",
                    importance=3,
                    source_rounds=[1],
                ),
                decision=MemoryConsolidationDecision(
                    action="deduplicate",
                    target_memory_id=duplicate.id,
                ),
                source_message_ids=source_ids[1],
            ),
            MemoryConsolidationPlan(
                candidate=MemoryCandidate(
                    type="角色承诺",
                    content="艾拉承诺陪用户寻找线索。",
                    importance=4,
                    source_rounds=[2],
                ),
                decision=MemoryConsolidationDecision(
                    action="update",
                    target_memory_id=updated.id,
                    content="艾拉承诺陪用户寻找线索。",
                    importance=4,
                ),
                source_message_ids=source_ids[2],
            ),
            MemoryConsolidationPlan(
                candidate=MemoryCandidate(
                    type="关系变化",
                    content="双方已经建立信任。",
                    importance=5,
                    source_rounds=[3],
                ),
                decision=MemoryConsolidationDecision(
                    action="invalidate",
                    target_memory_id=invalidated.id,
                ),
                source_message_ids=source_ids[3],
            ),
            MemoryConsolidationPlan(
                candidate=MemoryCandidate(
                    type="重要剧情",
                    content="找到了旧档案馆。",
                    importance=5,
                    source_rounds=[4],
                ),
                decision=MemoryConsolidationDecision(
                    action="create",
                    content="找到了旧档案馆。",
                    importance=5,
                ),
                source_message_ids=source_ids[4],
            ),
            MemoryConsolidationPlan(
                candidate=MemoryCandidate(
                    type="重要剧情",
                    content="普通寒暄。",
                    importance=1,
                    source_rounds=[5],
                ),
                decision=MemoryConsolidationDecision(action="ignore"),
                source_message_ids=source_ids[5],
            ),
        ]
        gateway = StubGateway([])
        chroma = RecordingChroma()
        service = MemoryService(session, gateway, chroma)

        service.persist_consolidation_plans(task.id, plans)

        created = session.scalar(
            select(LongTermMemory).where(LongTermMemory.content == "找到了旧档案馆。")
        )
        assert created is not None
        assert session.get(LongTermMemory, duplicate.id).memory_version == 1
        refreshed_updated = session.get(LongTermMemory, updated.id)
        refreshed_invalidated = session.get(LongTermMemory, invalidated.id)
        assert refreshed_updated is not None and refreshed_updated.memory_version == 2
        assert refreshed_updated.index_status == "pending"
        assert refreshed_invalidated is not None and refreshed_invalidated.status == "invalid"
        assert session.scalar(select(func.count(MemorySource.memory_id))) == 8
        assert chat_session.consolidated_round == 0

        asyncio.run(service.synchronize_and_finish(task.id))

        session.expire_all()
        refreshed_task = session.get(BackgroundTask, task.id)
        refreshed_session = session.get(ChatSession, chat_session.id)
        refreshed_updated = session.get(LongTermMemory, updated.id)
        refreshed_created = session.get(LongTermMemory, created.id)
        assert refreshed_task is not None and refreshed_task.status == "succeeded"
        assert (refreshed_task.progress_current, refreshed_task.progress_total) == (10, 10)
        assert refreshed_session is not None and refreshed_session.consolidated_round == 10
        assert refreshed_updated is not None and refreshed_updated.index_status == "ready"
        assert refreshed_created is not None and refreshed_created.embedding_version == 2
        assert set(chroma.upserts[0]["ids"]) == {
            memory_document_id(updated.id),
            memory_document_id(created.id),
        }
        assert chroma.deleted_ids == [memory_document_id(invalidated.id)]
        assert duplicate.id not in chroma.upserts[0]["ids"]
    finally:
        session.close()
        engine.dispose()


def test_memory_task_retry_repairs_index_without_repeating_model_plan(tmp_path: Path) -> None:
    """索引失败后同一任务复用来源凭据修复，不重复新增或增加版本。"""

    session, engine = create_database(tmp_path)
    try:
        character, chat_session, task, _source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        configure_models(session)
        existing = add_memory(
            session,
            character,
            content="艾拉答应提供线索。",
            memory_type="角色承诺",
        )
        gateway = StubGateway(
            [
                (
                    '{"candidates":[{"type":"角色承诺",'
                    '"content":"艾拉承诺陪用户寻找线索。",'
                    '"importance":4,"sourceRounds":[4]}]}'
                ),
                (
                    '{"action":"update","targetMemoryId":"'
                    + existing.id
                    + '","content":"艾拉承诺陪用户寻找线索。","importance":4}'
                ),
            ]
        )
        gateway.embedding_error_on_call = 2
        chroma = RecordingChroma()
        chroma.result = {
            "ids": [[memory_document_id(existing.id)]],
            "distances": [[0.1]],
            "metadatas": [[memory_metadata(existing)]],
        }
        runner = MemoryTask(session, gateway, chroma)

        assert asyncio.run(runner.run(task.id)) is False

        session.expire_all()
        failed_task = session.get(BackgroundTask, task.id)
        memory = session.scalar(select(LongTermMemory))
        assert failed_task is not None and failed_task.status == "failed"
        assert memory is not None and memory.index_status == "failed"
        assert memory.memory_version == 2
        assert session.get(ChatSession, chat_session.id).consolidated_round == 0
        assert len(gateway.chat_requests) == 2

        assert retry_failed_memory_task(session, task.id) is True
        gateway.embedding_error_on_call = None
        assert asyncio.run(runner.run(task.id)) is True

        session.expire_all()
        repaired_task = session.get(BackgroundTask, task.id)
        repaired_memory = session.get(LongTermMemory, memory.id)
        assert repaired_task is not None and repaired_task.status == "succeeded"
        assert repaired_memory is not None and repaired_memory.index_status == "ready"
        assert repaired_memory.memory_version == 2
        assert session.scalar(select(func.count(LongTermMemory.id))) == 1
        assert session.get(ChatSession, chat_session.id).consolidated_round == 10
        assert len(gateway.chat_requests) == 2
    finally:
        session.close()
        engine.dispose()


def test_invalidation_delete_failure_records_cleanup_and_does_not_advance(
    tmp_path: Path,
) -> None:
    """自动失效向量删除失败时记录补偿任务，并保持整理进度不变。"""

    session, engine = create_database(tmp_path)
    try:
        character, chat_session, task, source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        configure_models(session)
        invalidated = add_memory(
            session,
            character,
            content="双方仍互不信任。",
            memory_type="关系变化",
        )
        service = MemoryService(session, StubGateway([]), RecordingChroma())
        service.persist_consolidation_plans(
            task.id,
            [
                MemoryConsolidationPlan(
                    candidate=MemoryCandidate(
                        type="关系变化",
                        content="双方已经建立信任。",
                        importance=5,
                        source_rounds=[3],
                    ),
                    decision=MemoryConsolidationDecision(
                        action="invalidate",
                        target_memory_id=invalidated.id,
                    ),
                    source_message_ids=source_ids[3],
                )
            ],
        )
        chroma = RecordingChroma()
        chroma.delete_error = RuntimeError("delete failed")
        runner = MemoryTask(session, StubGateway([]), chroma)

        assert asyncio.run(runner.run(task.id)) is False

        session.expire_all()
        failed_task = session.get(BackgroundTask, task.id)
        refreshed_memory = session.get(LongTermMemory, invalidated.id)
        cleanup_task = session.scalar(
            select(BackgroundTask).where(BackgroundTask.task_type == "vector_cleanup")
        )
        assert failed_task is not None and failed_task.status == "failed"
        assert refreshed_memory is not None and refreshed_memory.status == "invalid"
        assert cleanup_task is not None and cleanup_task.scope_id == invalidated.id
        assert session.get(ChatSession, chat_session.id).consolidated_round == 0

        chroma.delete_error = None
        assert retry_failed_memory_task(session, task.id) is True
        assert asyncio.run(runner.run(task.id)) is True

        session.expire_all()
        completed_task = session.get(BackgroundTask, task.id)
        completed_cleanup = session.get(BackgroundTask, cleanup_task.id)
        assert completed_task is not None and completed_task.status == "succeeded"
        assert completed_cleanup is not None and completed_cleanup.status == "succeeded"
        assert session.get(ChatSession, chat_session.id).consolidated_round == 10
    finally:
        session.close()
        engine.dispose()


def test_pending_runner_processes_same_session_backlog_in_order(tmp_path: Path) -> None:
    """独立 Session 调度器按顺序完成同会话目标 10、20 和 30。"""

    session, engine = create_database(tmp_path)
    try:
        _character, chat_session, _task, _source_ids = add_task_context(
            session,
            round_count=30,
            consolidated_round=0,
            target_round=10,
        )
        configure_models(session)
        session.close()
        engine.dispose()

        settings = Settings(environment="test", data_dir=tmp_path)
        gateway = StubGateway(
            ['{"candidates":[]}', '{"candidates":[]}', '{"candidates":[]}']
        )
        asyncio.run(run_pending_memory_tasks(settings, gateway, RecordingChroma()))

        verify_engine = create_database_engine(settings)
        try:
            with create_session_factory(verify_engine)() as verify_session:
                refreshed_session = verify_session.get(ChatSession, chat_session.id)
                tasks = verify_session.scalars(
                    select(BackgroundTask)
                    .where(BackgroundTask.task_type == "memory_consolidation")
                    .order_by(BackgroundTask.target_version)
                ).all()
                assert refreshed_session is not None
                assert refreshed_session.consolidated_round == 30
                assert [(item.target_version, item.status) for item in tasks] == [
                    (10, "succeeded"),
                    (20, "succeeded"),
                    (30, "succeeded"),
                ]
        finally:
            verify_engine.dispose()
    finally:
        if session.is_active:
            session.close()
        engine.dispose()


def test_recover_interrupted_memory_task_returns_it_to_pending(tmp_path: Path) -> None:
    """应用重启时 running 任务恢复为可重新领取的 pending。"""

    session, engine = create_database(tmp_path)
    try:
        _character, _chat_session, task, _source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        task.status = "running"
        task.started_at = "2026-08-11T00:00:00Z"
        session.commit()

        assert recover_interrupted_memory_tasks(session) == 1

        session.expire_all()
        recovered = session.get(BackgroundTask, task.id)
        assert recovered is not None and recovered.status == "pending"
        assert recovered.started_at is None
        assert recovered.finished_at is None
    finally:
        session.close()
        engine.dispose()


def test_application_startup_recovers_and_schedules_pending_memory_tasks(
    tmp_path: Path,
) -> None:
    """应用生命周期恢复 running 任务后立即启动非阻塞 pending 调度。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    session, engine = create_database(tmp_path)
    try:
        _character, _chat_session, task, _source_ids = add_task_context(
            session,
            round_count=10,
            consolidated_round=0,
            target_round=10,
        )
        task.status = "running"
        task.started_at = "2026-08-11T00:00:00Z"
        session.commit()
        task_id = task.id
    finally:
        session.close()
        engine.dispose()

    with patch("app.main.run_pending_memory_tasks", new_callable=AsyncMock) as scheduler:
        with TestClient(create_app(settings)) as client:
            assert client.get("/api/health").status_code == 200

        scheduler.assert_awaited_once()

    verify_engine = create_database_engine(settings)
    try:
        with create_session_factory(verify_engine)() as verify_session:
            recovered = verify_session.get(BackgroundTask, task_id)
            assert recovered is not None and recovered.status == "pending"
            assert recovered.started_at is None
    finally:
        verify_engine.dispose()
