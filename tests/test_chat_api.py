"""阶段 5–7 会话、SSE 与长期记忆 API 集成测试。"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.api.system import get_chroma_repository, get_model_gateway
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import protect_api_key
from app.db.database import (
    create_database_engine,
    create_session_factory,
    get_db_session,
)
from app.db.models import BackgroundTask, ChatSession, LongTermMemory, ModelConfig
from app.gateways.model_gateway import ModelStreamChunk
from app.main import create_app
from app.repositories.chroma_repository import (
    calculate_content_hash,
    memory_document_id,
)
from app.retrievers.memory_retriever import MemoryRetriever
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from scripts.init_db import initialize_database


class UnusedGateway:
    """空世界书会话不应调用 Embedding。"""

    async def create_embeddings(self, **_kwargs):
        """若被调用则说明空世界书错误触发了索引。"""

        raise AssertionError("空世界书不应生成快照向量")

    async def create_chat_completion(self, **_kwargs) -> str:
        """返回推荐回复等非流式任务的确定结果。"""

        messages = _kwargs["messages"]
        if "用户推荐回复规则" in messages[-1]["content"]:
            return '{"content":"我们继续前进吧。"}'
        raise AssertionError("基础角色回复必须使用流式模型调用")

    async def stream_chat_completion(self, **_kwargs):
        """返回两块确定的基础角色回复。"""

        yield ModelStreamChunk(
            "content",
            '{"blocks":['
            '{"type":"action","content":"她看向门口。"},'
            '{"type":"dialogue","content":"欢迎回来。"}'
            "]}",
        )


class RecordingChroma:
    """空世界书 API 测试使用的最小 Chroma 替身。"""

    def upsert_worldbook(self, **_kwargs) -> None:
        """空世界书不应写入向量。"""

        raise AssertionError("空世界书不应写入向量")

    def delete_worldbook(self, *, ids: list[str]) -> None:
        """没有快照向量时不会触发删除。"""

        assert ids == []

    def delete_memories(self, *, ids: list[str]) -> None:
        """记忆 API 测试允许按确定 ID 清理向量。"""

        assert all(document_id.startswith("memory:") for document_id in ids)

    def query_memories(self, **_kwargs):
        """没有 SQLite 候选时 Retriever 不应访问 Chroma。"""

        raise AssertionError("失效记忆不应进入向量查询")


def create_chat_client(
    tmp_path: Path,
) -> tuple[TestClient, Engine, sessionmaker[Session]]:
    """创建使用隔离 SQLite 的真实会话 API 客户端。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    app = create_app(settings)

    def override_db_session():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_model_gateway] = UnusedGateway
    app.dependency_overrides[get_chroma_repository] = RecordingChroma
    return TestClient(app), engine, session_factory


def configure_main_model(session_factory: sessionmaker[Session]) -> None:
    """为真实 SSE API 配置可用主模型。"""

    with session_factory() as session:
        config = session.get(ModelConfig, "main")
        assert config is not None
        config.base_url = "https://models.example/v1"
        config.model_name = "chat-model"
        config.secret_ref = protect_api_key("sk-main-test")
        session.commit()


def test_session_api_uses_real_character_and_world_book_ids(tmp_path: Path) -> None:
    """新会话可直接绑定真实内容 API 创建的角色卡与世界书。"""

    client, engine, session_factory = create_chat_client(tmp_path)
    try:
        identity = client.put(
            "/api/identity",
            json={"name": "Strand", "personaName": "master"},
        ).json()["data"]
        character = client.post("/api/characters", json={}).json()["data"]
        character = client.put(
            f"/api/characters/{character['id']}",
            json={
                "name": "Isra",
                "dialogueExamples": [{"user": "启动", "assistant": "系统已重新上线。"}],
            },
        ).json()["data"]
        world_book = client.post("/api/worldbooks", json={}).json()["data"]
        world_book = client.put(
            f"/api/worldbooks/{world_book['id']}",
            json={"name": "魔法人偶"},
        ).json()["data"]

        created_response = client.post(
            "/api/sessions",
            json={
                "characterId": character["id"],
                "worldBookId": world_book["id"],
            },
        )
        assert created_response.status_code == 201
        created = created_response.json()["data"]
        assert created["characterId"] == character["id"]
        assert created["worldBookId"] == world_book["id"]
        assert created["identityName"] == identity["name"]
        assert created["identityPersonaName"] == identity["personaName"]
        assert created["characterName"] == "Isra"
        assert created["worldBookName"] == "魔法人偶"

        messages = client.get(f"/api/sessions/{created['id']}/messages")
        assert messages.status_code == 200
        assert messages.json()["data"][0]["blocks"][0]["content"] == "系统已重新上线。"

        client.put("/api/identity", json={"name": "修改后的身份"})
        client.put(f"/api/characters/{character['id']}", json={"name": "修改后的角色"})
        client.put(
            f"/api/worldbooks/{world_book['id']}", json={"name": "修改后的世界书"}
        )
        # 会话不再冻结上下文，改名后已有会话立即反映最新绑定
        refreshed = client.get("/api/sessions").json()["data"][0]
        assert refreshed["identityName"] == "修改后的身份"
        assert refreshed["characterName"] == "修改后的角色"
        assert refreshed["worldBookName"] == "修改后的世界书"

        second = client.post(
            "/api/sessions",
            json={
                "characterId": character["id"],
                "worldBookId": world_book["id"],
            },
        ).json()["data"]
        with session_factory() as session:
            first_model = session.get(ChatSession, created["id"])
            second_model = session.get(ChatSession, second["id"])
            assert first_model is not None and second_model is not None
            first_model.updated_at = "2026-08-10T10:00:00.000Z"
            second_model.updated_at = "2026-08-10T11:00:00.000Z"
            session.commit()
        assert client.get("/api/sessions").json()["data"][0]["id"] == second["id"]

        renamed = client.patch(
            f"/api/sessions/{created['id']}",
            json={"title": "  与 Isra 的新故事  "},
        )
        assert renamed.status_code == 200
        assert renamed.json()["data"]["title"] == "与 Isra 的新故事"
        assert client.get("/api/sessions").json()["data"][0]["id"] == created["id"]

        deleted = client.delete(f"/api/sessions/{created['id']}")
        assert deleted.status_code == 204
        assert client.delete(f"/api/sessions/{second['id']}").status_code == 204
        assert client.get("/api/sessions").json()["data"] == []
    finally:
        client.close()
        engine.dispose()


def test_message_actions_and_recommended_reply_api(tmp_path: Path) -> None:
    """消息操作复用 SSE，推荐回复只返回草稿且不新增消息。"""

    client, engine, session_factory = create_chat_client(tmp_path)
    try:
        configure_main_model(session_factory)
        character = client.post("/api/characters", json={}).json()["data"]
        character = client.put(
            f"/api/characters/{character['id']}",
            json={
                "name": "艾拉",
                "dialogueExamples": [{"user": "你好", "assistant": "你终于来了。"}],
            },
        ).json()["data"]
        created = client.post(
            "/api/sessions",
            json={"characterId": character["id"], "worldBookId": None},
        ).json()["data"]
        sent = client.post(
            f"/api/sessions/{created['id']}/messages",
            json={"content": "我们出发吧。"},
        )
        assert sent.status_code == 200
        messages = client.get(f"/api/sessions/{created['id']}/messages").json()["data"]
        assistant_id = messages[-1]["id"]

        recommended = client.post(f"/api/sessions/{created['id']}/recommended-reply")
        assert recommended.status_code == 200
        assert recommended.json()["data"]["content"] == "我们继续前进吧。"
        assert len(client.get(f"/api/sessions/{created['id']}/messages").json()["data"]) == 3

        regenerated = client.post(
            f"/api/sessions/{created['id']}/messages/{assistant_id}/regenerate"
        )
        assert regenerated.status_code == 200
        assert '"type":"done"' in regenerated.text
        after_regenerate = client.get(
            f"/api/sessions/{created['id']}/messages"
        ).json()["data"][-1]
        assert after_regenerate["id"] == assistant_id
        assert len(after_regenerate["blocks"]) == 2

        continued = client.post(
            f"/api/sessions/{created['id']}/messages/{assistant_id}/continue"
        )
        assert continued.status_code == 200
        after_continue = client.get(
            f"/api/sessions/{created['id']}/messages"
        ).json()["data"][-1]
        assert after_continue["id"] == assistant_id
        assert [block["sequence"] for block in after_continue["blocks"]] == [0, 1, 2, 3]

        deleted = client.delete(f"/api/sessions/{created['id']}/turns/{assistant_id}")
        assert deleted.status_code == 204
        remaining = client.get(f"/api/sessions/{created['id']}/messages").json()["data"]
        assert [message["role"] for message in remaining] == ["assistant"]
        assert client.get("/api/sessions").json()["data"][0]["roundCount"] == 0
    finally:
        client.close()
        engine.dispose()


def test_session_api_returns_validation_and_real_not_found_errors(
    tmp_path: Path,
) -> None:
    """会话 API 对无效字段和不存在的真实资源返回稳定错误。"""

    client, engine, _session_factory = create_chat_client(tmp_path)
    try:
        missing_character = client.post(
            "/api/sessions",
            json={"characterId": "missing", "worldBookId": None},
        )
        assert missing_character.status_code == 404
        assert missing_character.json()["message"] == "角色卡不存在"

        invalid_title = client.patch(
            "/api/sessions/missing",
            json={"title": "  "},
        )
        assert invalid_title.status_code == 422
        assert invalid_title.json()["code"] == "VALIDATION_ERROR"

        missing_messages = client.get("/api/sessions/missing/messages")
        assert missing_messages.status_code == 404
        assert missing_messages.json()["message"] == "会话不存在"
    finally:
        client.close()
        engine.dispose()


def test_message_api_streams_and_persists_ordered_role_reply(tmp_path: Path) -> None:
    """SSE 按块返回回复，并以服务端结果保存完整一轮对话。"""

    client, engine, session_factory = create_chat_client(tmp_path)
    try:
        character = client.post("/api/characters", json={}).json()["data"]
        character = client.put(
            f"/api/characters/{character['id']}",
            json={
                "name": "Isra",
                "dialogueExamples": [{"user": "启动", "assistant": "系统已上线。"}],
            },
        ).json()["data"]
        created = client.post(
            "/api/sessions",
            json={"characterId": character["id"], "worldBookId": None},
        ).json()["data"]
        configure_main_model(session_factory)

        response = client.post(
            f"/api/sessions/{created['id']}/messages",
            json={"content": "我回来了。"},
            headers={"Accept": "text/event-stream"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = [
            json.loads(frame.removeprefix("data: "))
            for frame in response.text.strip().split("\n\n")
            if frame.startswith("data: ")
        ]
        assert [event["type"] for event in events] == [
            "block_start",
            "block_delta",
            "block_end",
            "block_start",
            "block_delta",
            "block_end",
            "done",
        ]
        assert events[0]["blockType"] == "action"
        assert events[1]["text"] == "她看向门口。"
        assert events[3]["blockType"] == "dialogue"
        assert events[4]["text"] == "欢迎回来。"
        assert events[-1]["roundCount"] == 1

        messages = client.get(f"/api/sessions/{created['id']}/messages").json()["data"]
        assert [message["role"] for message in messages] == [
            "assistant",
            "user",
            "assistant",
        ]
        assert [block["type"] for block in messages[-1]["blocks"]] == [
            "action",
            "dialogue",
        ]
        assert [block["content"] for block in messages[-1]["blocks"]] == [
            "她看向门口。",
            "欢迎回来。",
        ]
    finally:
        client.close()
        engine.dispose()


def test_message_api_schedules_memory_runner_after_sse(tmp_path: Path) -> None:
    """SSE 完整结束后使用独立后台入口扫描待处理记忆任务。"""

    client, engine, session_factory = create_chat_client(tmp_path)
    try:
        character = client.post("/api/characters", json={}).json()["data"]
        created = client.post(
            "/api/sessions",
            json={"characterId": character["id"], "worldBookId": None},
        ).json()["data"]
        configure_main_model(session_factory)

        with patch(
            "app.api.chat.run_pending_memory_tasks",
            new_callable=AsyncMock,
        ) as scheduler:
            response = client.post(
                f"/api/sessions/{created['id']}/messages",
                json={"content": "继续。"},
            )

        assert response.status_code == 200
        scheduler.assert_awaited_once()
    finally:
        client.close()
        engine.dispose()


def test_message_api_rejects_unconfigured_main_model_before_saving_user(
    tmp_path: Path,
) -> None:
    """主模型未配置时返回普通错误，且不把本次用户输入写入历史。"""

    client, engine, _session_factory = create_chat_client(tmp_path)
    try:
        character = client.post("/api/characters", json={}).json()["data"]
        created = client.post(
            "/api/sessions",
            json={"characterId": character["id"], "worldBookId": None},
        ).json()["data"]

        response = client.post(
            f"/api/sessions/{created['id']}/messages",
            json={"content": "这条消息不应入库。"},
        )

        assert response.status_code == 503
        assert response.json()["code"] == "MODEL_NOT_CONFIGURED"
        assert (
            client.get(f"/api/sessions/{created['id']}/messages").json()["data"] == []
        )
    finally:
        client.close()
        engine.dispose()


def test_message_api_rejects_required_index_rebuild_before_saving_user(
    tmp_path: Path,
) -> None:
    """全局索引待重建时返回普通 JSON 错误，且不保存本轮用户消息。"""

    client, engine, session_factory = create_chat_client(tmp_path)
    try:
        character = client.post("/api/characters", json={}).json()["data"]
        created = client.post(
            "/api/sessions",
            json={"characterId": character["id"], "worldBookId": None},
        ).json()["data"]
        configure_main_model(session_factory)
        with session_factory() as session:
            config = session.get(ModelConfig, "embed")
            assert config is not None
            config.rebuild_required = 1
            session.commit()

        response = client.post(
            f"/api/sessions/{created['id']}/messages",
            json={"content": "这条消息不应入库。"},
        )

        assert response.status_code == 503
        assert response.json()["code"] == "INDEX_REBUILD_REQUIRED"
        assert (
            client.get(f"/api/sessions/{created['id']}/messages").json()["data"] == []
        )
    finally:
        client.close()
        engine.dispose()


def test_memory_api_filters_orders_and_invalidates_idempotently(tmp_path: Path) -> None:
    """真实 API 支持三类筛选、倒序返回，并只清理一次失效记忆向量。"""

    client, engine, session_factory = create_chat_client(tmp_path)
    try:
        first_character = client.post("/api/characters", json={}).json()["data"]
        second_character = client.post("/api/characters", json={}).json()["data"]
        memory_rows = [
            (
                "mem_role_one_new",
                first_character["id"],
                "用户偏好",
                "用户喜欢雨天。",
                "active",
                "2026-08-11T10:03:00+00:00",
            ),
            (
                "mem_role_two_target",
                second_character["id"],
                "角色承诺",
                "角色答应守住秘密。",
                "active",
                "2026-08-11T10:02:00+00:00",
            ),
            (
                "mem_role_one_invalid",
                first_character["id"],
                "用户偏好",
                "用户曾经喜欢甜食。",
                "invalid",
                "2026-08-11T10:01:00+00:00",
            ),
            (
                "mem_role_one_plot",
                first_character["id"],
                "重要剧情",
                "二人在旧桥重逢。",
                "active",
                "2026-08-11T10:00:00+00:00",
            ),
        ]
        with session_factory() as session:
            session.add_all(
                [
                    LongTermMemory(
                        id=memory_id,
                        character_id=character_id,
                        type=memory_type,
                        content=content,
                        importance=4,
                        status=memory_status,
                        source_label="测试会话第 1–10 轮",
                        memory_version=1,
                        index_status="ready",
                        embedding_version=1,
                        content_hash=calculate_content_hash(content),
                        created_at=created_at,
                        updated_at=created_at,
                    )
                    for (
                        memory_id,
                        character_id,
                        memory_type,
                        content,
                        memory_status,
                        created_at,
                    ) in memory_rows
                ]
            )
            session.commit()

        all_memories = client.get("/api/memories").json()["data"]
        assert [memory["id"] for memory in all_memories] == [
            row[0] for row in memory_rows
        ]
        role_and_type = client.get(
            "/api/memories",
            params={"characterId": first_character["id"], "type": "用户偏好"},
        ).json()["data"]
        assert [memory["id"] for memory in role_and_type] == [
            "mem_role_one_new",
            "mem_role_one_invalid",
        ]
        invalid_memories = client.get(
            "/api/memories",
            params={"status": "已失效"},
        ).json()["data"]
        assert [memory["id"] for memory in invalid_memories] == ["mem_role_one_invalid"]
        assert client.get("/api/memories", params={"status": "未知"}).status_code == 422

        with patch.object(
            RecordingChroma,
            "delete_memories",
            autospec=True,
        ) as delete_memories:
            first_response = client.post(
                "/api/memories/mem_role_two_target/invalidate",
                json={},
            )
            second_response = client.post(
                "/api/memories/mem_role_two_target/invalidate",
                json={},
            )

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert first_response.json()["data"]["status"] == "已失效"
        assert second_response.json()["data"] == first_response.json()["data"]
        delete_memories.assert_called_once()
        assert delete_memories.call_args.kwargs == {
            "ids": [memory_document_id("mem_role_two_target")]
        }
        assert (
            client.post("/api/memories/missing/invalidate", json={}).status_code == 404
        )

        with session_factory() as session:
            memory = session.get(LongTermMemory, "mem_role_two_target")
            assert memory is not None
            assert memory.status == "invalid"
            assert memory.index_status == "stale"
            assert memory.embedding_version is None
            assert (
                MemoryRetriever(session, RecordingChroma()).retrieve(
                    character_id=second_character["id"],
                    query_embedding=[0.1, 0.2],
                    embedding_version=1,
                )
                == []
            )
    finally:
        client.close()
        engine.dispose()


def test_memory_invalidation_keeps_sqlite_state_when_vector_delete_fails(
    tmp_path: Path,
) -> None:
    """向量删除失败时 API 仍成功，并为确定记忆 ID 创建补偿任务。"""

    client, engine, session_factory = create_chat_client(tmp_path)
    try:
        character = client.post("/api/characters", json={}).json()["data"]
        content = "用户把旧钥匙交给了角色。"
        with session_factory() as session:
            session.add(
                LongTermMemory(
                    id="mem_cleanup_required",
                    character_id=character["id"],
                    type="重要剧情",
                    content=content,
                    importance=5,
                    status="active",
                    source_label="测试会话第 1–10 轮",
                    memory_version=1,
                    index_status="ready",
                    embedding_version=1,
                    content_hash=calculate_content_hash(content),
                )
            )
            session.commit()

        with patch.object(
            RecordingChroma,
            "delete_memories",
            autospec=True,
            side_effect=RuntimeError("chroma unavailable"),
        ):
            response = client.post(
                "/api/memories/mem_cleanup_required/invalidate",
                json={},
            )

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "已失效"
        with session_factory() as session:
            memory = session.get(LongTermMemory, "mem_cleanup_required")
            assert memory is not None and memory.status == "invalid"
            cleanup_tasks = (
                session.query(BackgroundTask)
                .filter_by(
                    task_type="vector_cleanup",
                    scope_id=memory_document_id("mem_cleanup_required"),
                )
                .all()
            )
            assert len(cleanup_tasks) == 1
            assert cleanup_tasks[0].status == "failed"
            assert content not in (cleanup_tasks[0].error_message or "")
    finally:
        client.close()
        engine.dispose()


class FailingReplyGateway(UnusedGateway):
    """模拟主模型生成失败，使用户消息落库后没有配对回复。"""

    async def stream_chat_completion(self, **_kwargs):
        """建立流之后再失败，复现用户已看到消息发出的断层场景。"""

        raise AppError(
            status_code=502,
            code="MODEL_OUTPUT_INVALID",
            message="主模型返回的内容块结构无效",
        )
        yield  # pragma: no cover - 保持异步生成器签名


def test_unanswered_message_api_exposes_and_deletes_dangling_message(tmp_path: Path) -> None:
    """回复失败留下的用户消息带 unanswered 标记，并可通过消息接口单独删除。"""

    client, engine, session_factory = create_chat_client(tmp_path)
    try:
        configure_main_model(session_factory)
        character = client.post("/api/characters", json={}).json()["data"]
        character = client.put(
            f"/api/characters/{character['id']}",
            json={
                "name": "艾拉",
                "dialogueExamples": [{"user": "你好", "assistant": "你终于来了。"}],
            },
        ).json()["data"]
        created = client.post(
            "/api/sessions",
            json={"characterId": character["id"], "worldBookId": None},
        ).json()["data"]

        client.app.dependency_overrides[get_model_gateway] = FailingReplyGateway
        failed = client.post(
            f"/api/sessions/{created['id']}/messages",
            json={"content": "在吗？"},
        )
        assert failed.status_code == 200
        assert '"type":"error"' in failed.text

        messages = client.get(f"/api/sessions/{created['id']}/messages").json()["data"]
        assert [message["role"] for message in messages] == ["assistant", "user"]
        assert messages[0]["unanswered"] is False
        assert messages[1]["unanswered"] is True
        dangling_id = messages[1]["id"]

        # 完整轮次里的消息不能走这个接口
        client.app.dependency_overrides[get_model_gateway] = UnusedGateway
        client.post(f"/api/sessions/{created['id']}/messages", json={"content": "我们出发吧。"})
        complete = client.get(f"/api/sessions/{created['id']}/messages").json()["data"]
        rejected = client.delete(
            f"/api/sessions/{created['id']}/messages/{complete[-1]['id']}"
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "MESSAGE_NOT_DELETABLE"

        missing = client.delete(f"/api/sessions/{created['id']}/messages/not-a-real-message")
        assert missing.status_code == 404
        assert missing.json()["code"] == "RESOURCE_NOT_FOUND"

        deleted = client.delete(f"/api/sessions/{created['id']}/messages/{dangling_id}")
        assert deleted.status_code == 204
        remaining = client.get(f"/api/sessions/{created['id']}/messages").json()["data"]
        assert [message["role"] for message in remaining] == ["assistant", "user", "assistant"]
        assert all(message["unanswered"] is False for message in remaining)
        assert client.get("/api/sessions").json()["data"][0]["roundCount"] == 1
    finally:
        client.close()
        engine.dispose()
