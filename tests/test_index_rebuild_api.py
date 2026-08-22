"""阶段 2B Embedding 版本、双 Collection 重建与任务状态集成测试。"""

from pathlib import Path

import pytest
from app.api.system import get_model_gateway
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import protect_api_key
from app.db.database import (
    create_database_engine,
    create_session_factory,
    get_db_session,
)
from app.db.models import (
    BackgroundTask,
    Character,
    ChatSession,
    LongTermMemory,
    ModelConfig,
    WorldBook,
    WorldBookEntry,
)
from app.main import create_app
from app.repositories.chroma_repository import (
    LONG_TERM_MEMORY_COLLECTION,
    WORLD_BOOK_COLLECTION,
    ChromaRepository,
    build_worldbook_index_text,
    calculate_content_hash,
    memory_document_id,
    worldbook_entry_document_id,
)
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from scripts.init_db import initialize_database


class FakeEmbeddingGateway:
    """为重建测试返回固定维度向量或可控失败。"""

    def __init__(self) -> None:
        self.error: AppError | None = None
        self.calls: list[list[str]] = []

    async def create_embeddings(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        texts: list[str],
        expected_dimension: int | None = None,
    ) -> list[list[float]]:
        assert base_url == "https://models.example/v1"
        assert model == "embed-v2"
        assert api_key == "sk-rebuild-secret"
        assert expected_dimension == 3
        self.calls.append(texts)
        if self.error is not None:
            raise self.error
        return [[float(index + 1), 0.5, 0.25] for index, _text in enumerate(texts)]


def create_rebuild_client(
    tmp_path: Path,
    gateway: FakeEmbeddingGateway,
) -> tuple[TestClient, Engine, sessionmaker[Session], ChromaRepository]:
    """创建 SQLite、Chroma 和网关均隔离的 FastAPI 客户端。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    app = create_app(settings)

    def override_db_session():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_model_gateway] = lambda: gateway
    return TestClient(app), engine, session_factory, ChromaRepository(settings)


def seed_rebuild_sources(session_factory: sessionmaker[Session]) -> dict[str, str]:
    """写入正式条目、会话快照、有效记忆和一条失效记忆。"""

    with session_factory.begin() as session:
        config = session.get(ModelConfig, "embed")
        assert config is not None
        config.base_url = "https://models.example/v1"
        config.model_name = "embed-v2"
        config.secret_ref = protect_api_key("sk-rebuild-secret")
        config.key_tail = "cret"
        config.vector_dimension = 3
        config.config_version = 2
        config.rebuild_required = 1

        character = Character(name="守夜人")
        book = WorldBook(name="雾港志")
        session.add_all([character, book])
        session.flush()

        entry = WorldBookEntry(
            world_book_id=book.id,
            name="雾港",
            content="终年被海雾笼罩。",
            keywords_json='["港口", "海雾"]',
            category="地点",
            resident=0,
            enabled=1,
            index_status="stale",
            embedding_version=1,
            content_hash="old-live-hash",
        )
        chat_session = ChatSession(
            title="初访雾港",
            character_id=character.id,
            world_book_id=book.id,
        )
        session.add_all([entry, chat_session])
        session.flush()

        active_memory = LongTermMemory(
            character_id=character.id,
            type="重要剧情",
            content="用户答应在黎明前返回灯塔。",
            importance=4,
            status="active",
            source_label="初访雾港",
            index_status="stale",
            embedding_version=1,
            content_hash="old-memory-hash",
        )
        invalid_memory = LongTermMemory(
            character_id=character.id,
            type="长期目标",
            content="已取消的旧目标。",
            importance=2,
            status="invalid",
            source_label="旧会话",
            index_status="ready",
            embedding_version=1,
            content_hash=calculate_content_hash("已取消的旧目标。"),
        )
        session.add_all([active_memory, invalid_memory])
        session.flush()
        return {
            "entry": entry.id,
            "session": chat_session.id,
            "book": book.id,
            "memory": active_memory.id,
            "invalid_memory": invalid_memory.id,
            "character": character.id,
        }


def seed_orphan_vectors(repository: ChromaRepository) -> None:
    """写入不再存在于 SQLite 的旧向量，验证全量重建会清理它们。"""

    repository.upsert_worldbook(
        ids=["worldbook-entry:orphan"],
        embeddings=[[1.0, 0.0, 0.0]],
        documents=["旧世界书向量"],
        metadatas=[
            {
                "sourceKind": "liveEntry",
                "entryId": "orphan",
                "worldBookId": "old-book",
                "enabled": True,
                "resident": False,
                "embeddingVersion": 1,
                "contentHash": "old",
            }
        ],
    )
    repository.upsert_memories(
        ids=["memory:orphan"],
        embeddings=[[0.0, 1.0, 0.0]],
        documents=["旧记忆向量"],
        metadatas=[
            {
                "memoryId": "orphan",
                "characterId": "old-character",
                "status": "active",
                "memoryType": "重要剧情",
                "embeddingVersion": 1,
                "contentHash": "old",
            }
        ],
    )


def test_rebuild_replaces_both_collections_and_marks_sources_ready(
    tmp_path: Path,
) -> None:
    """三类事实数据使用同一目标版本重建，旧向量与失效记忆均不保留。"""

    gateway = FakeEmbeddingGateway()
    client, engine, session_factory, chroma = create_rebuild_client(tmp_path, gateway)
    ids = seed_rebuild_sources(session_factory)
    seed_orphan_vectors(chroma)
    try:
        response = client.post("/api/index/rebuild", json={})
        assert response.status_code == 200
        assert response.json()["data"] == {"rebuildRequired": False}
        assert sum(len(batch) for batch in gateway.calls) == 2

        with session_factory() as session:
            config = session.get(ModelConfig, "embed")
            task = session.scalar(select(BackgroundTask))
            entry = session.get(WorldBookEntry, ids["entry"])
            memory = session.get(LongTermMemory, ids["memory"])
            invalid_memory = session.get(LongTermMemory, ids["invalid_memory"])
            assert config is not None and config.rebuild_required == 0
            assert task is not None
            assert (task.status, task.target_version) == ("succeeded", 2)
            assert (task.progress_current, task.progress_total) == (2, 2)
            assert entry is not None and memory is not None
            assert {
                (entry.index_status, entry.embedding_version),
                (memory.index_status, memory.embedding_version),
            } == {("ready", 2)}
            assert invalid_memory is not None
            assert (invalid_memory.index_status, invalid_memory.embedding_version) == (
                "ready",
                1,
            )

            live_text = build_worldbook_index_text(
                name=entry.name,
                category=entry.category,
                keywords=["港口", "海雾"],
                content=entry.content,
            )
            assert entry.content_hash == calculate_content_hash(live_text)
            assert memory.content_hash == calculate_content_hash(memory.content)

        world_collection = chroma.client.get_collection(
            WORLD_BOOK_COLLECTION,
            embedding_function=None,
        )
        memory_collection = chroma.client.get_collection(
            LONG_TERM_MEMORY_COLLECTION,
            embedding_function=None,
        )
        world_result = world_collection.get(include=["documents", "metadatas"])
        memory_result = memory_collection.get(include=["documents", "metadatas"])
        assert set(world_result["ids"]) == {worldbook_entry_document_id(ids["entry"])}
        assert memory_result["ids"] == [memory_document_id(ids["memory"])]
        assert memory_result["metadatas"][0]["embeddingVersion"] == 2
        assert memory_result["metadatas"][0]["characterId"] == ids["character"]
    finally:
        client.close()
        engine.dispose()


def test_rebuild_failure_keeps_flag_and_records_sanitized_failure(
    tmp_path: Path,
) -> None:
    """Embedding 失败不丢 SQLite 正文，并留下可重试的失败任务与状态。"""

    gateway = FakeEmbeddingGateway()
    gateway.error = AppError(
        status_code=502,
        code="MODEL_CONNECTION_FAILED",
        message="上游调用失败",
    )
    client, engine, session_factory, chroma = create_rebuild_client(tmp_path, gateway)
    ids = seed_rebuild_sources(session_factory)
    seed_orphan_vectors(chroma)
    try:
        response = client.post("/api/index/rebuild", json={})
        assert response.status_code == 500
        assert response.json()["code"] == "INDEX_OPERATION_FAILED"
        assert response.json()["message"] == "索引重建失败"

        with session_factory() as session:
            config = session.get(ModelConfig, "embed")
            task = session.scalar(select(BackgroundTask))
            entry = session.get(WorldBookEntry, ids["entry"])
            memory = session.get(LongTermMemory, ids["memory"])
            assert config is not None and config.rebuild_required == 1
            assert task is not None and task.status == "failed"
            assert task.error_message == "索引重建失败"
            assert entry is not None and entry.content == "终年被海雾笼罩。"
            assert (entry.index_status, entry.embedding_version) == ("failed", None)
            assert memory is not None and memory.index_status == "failed"

        old_world = chroma.client.get_collection(
            WORLD_BOOK_COLLECTION,
            embedding_function=None,
        )
        assert old_world.get()["ids"] == ["worldbook-entry:orphan"]
    finally:
        client.close()
        engine.dispose()


def test_rebuild_rejects_existing_active_task(tmp_path: Path) -> None:
    """已有 pending/running 重建任务时返回稳定 409，且不调用模型。"""

    gateway = FakeEmbeddingGateway()
    client, engine, session_factory, _chroma = create_rebuild_client(tmp_path, gateway)
    seed_rebuild_sources(session_factory)
    with session_factory.begin() as session:
        session.add(
            BackgroundTask(
                task_type="index_rebuild",
                status="running",
                target_version=2,
                started_at="2026-08-10T00:00:00Z",
            )
        )
    try:
        response = client.post("/api/index/rebuild", json={})
        assert response.status_code == 409
        assert response.json()["code"] == "TASK_ALREADY_RUNNING"
        assert gateway.calls == []
    finally:
        client.close()
        engine.dispose()


def test_rebuild_requires_saved_embedding_configuration(tmp_path: Path) -> None:
    """空配置不会创建任务或访问 Embedding 服务。"""

    gateway = FakeEmbeddingGateway()
    client, engine, session_factory, _chroma = create_rebuild_client(tmp_path, gateway)
    try:
        response = client.post("/api/index/rebuild", json={})
        assert response.status_code == 503
        assert response.json()["code"] == "MODEL_NOT_CONFIGURED"
        with session_factory() as session:
            assert session.scalar(select(BackgroundTask)) is None
        assert gateway.calls == []
    finally:
        client.close()
        engine.dispose()


@pytest.mark.parametrize("interrupted_status", ["pending", "running"])
def test_application_startup_recovers_interrupted_rebuild_and_allows_retry(
    tmp_path: Path,
    interrupted_status: str,
) -> None:
    """重启后遗留活动任务转为失败，并释放唯一约束供用户安全重试。"""

    gateway = FakeEmbeddingGateway()
    client, engine, session_factory, _chroma = create_rebuild_client(tmp_path, gateway)
    ids = seed_rebuild_sources(session_factory)
    with session_factory.begin() as session:
        config = session.get(ModelConfig, "embed")
        entry = session.get(WorldBookEntry, ids["entry"])
        assert config is not None and entry is not None
        config.rebuild_required = 0
        entry.index_status = "ready"
        entry.embedding_version = 1
        interrupted_task = BackgroundTask(
            task_type="index_rebuild",
            status=interrupted_status,
            target_version=2,
            started_at=(
                "2026-08-10T00:00:00Z" if interrupted_status == "running" else None
            ),
        )
        session.add(interrupted_task)
        session.flush()
        interrupted_task_id = interrupted_task.id

    try:
        with client:
            assert client.get("/api/index/status").json()["data"] == {
                "rebuildRequired": True
            }
            with session_factory() as session:
                recovered_task = session.get(BackgroundTask, interrupted_task_id)
                recovered_entry = session.get(WorldBookEntry, ids["entry"])
                assert recovered_task is not None
                assert recovered_task.status == "failed"
                assert recovered_task.error_message == "应用中断，索引重建任务未完成"
                assert recovered_task.finished_at is not None
                assert recovered_entry is not None
                assert (
                    recovered_entry.index_status,
                    recovered_entry.embedding_version,
                ) == (
                    "failed",
                    None,
                )

            retry_response = client.post("/api/index/rebuild", json={})
            assert retry_response.status_code == 200
            assert retry_response.json()["data"] == {"rebuildRequired": False}
    finally:
        client.close()
        engine.dispose()
