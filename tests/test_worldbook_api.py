"""阶段 4 世界书 REST API 集成测试。"""

from pathlib import Path

from app.api.system import get_chroma_repository, get_model_gateway
from app.core.config import Settings
from app.core.security import protect_api_key
from app.db.database import (
    create_database_engine,
    create_session_factory,
    get_db_session,
)
from app.db.models import ModelConfig
from app.main import create_app
from app.repositories.chroma_repository import worldbook_entry_document_id
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from scripts.init_db import initialize_database


class StubGateway:
    """为拆分与 Embedding API 提供确定响应。"""

    async def create_chat_completion(self, **_kwargs) -> str:
        """返回符合结构化契约的单条草稿。"""

        return '{"drafts":[{"name":"银港","category":"地点","content":"银港被海雾笼罩。"}]}'

    async def create_embeddings(self, **kwargs) -> list[list[float]]:
        """为每段索引文本返回三维测试向量。"""

        return [[0.1, 0.2, 0.3] for _text in kwargs["texts"]]


class RecordingChroma:
    """记录 API 触发的世界书向量写入与删除。"""

    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []
        self.deleted_ids: list[str] = []

    def upsert_worldbook(self, **kwargs) -> None:
        """记录向量写入。"""

        self.upserts.append(kwargs)

    def delete_worldbook(self, *, ids: list[str]) -> None:
        """记录确定 ID 删除。"""

        self.deleted_ids.extend(ids)


def create_worldbook_client(
    tmp_path: Path,
) -> tuple[TestClient, Engine, sessionmaker[Session], RecordingChroma]:
    """创建隔离数据库、模型网关与向量仓储的 API 客户端。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    app = create_app(settings)
    chroma = RecordingChroma()

    def override_db_session():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_model_gateway] = StubGateway
    app.dependency_overrides[get_chroma_repository] = lambda: chroma
    return TestClient(app), engine, session_factory, chroma


def configure_models(session_factory: sessionmaker[Session]) -> None:
    """配置可用的主模型与 Embedding 测试端点。"""

    with session_factory() as session:
        main = session.get(ModelConfig, "main")
        embed = session.get(ModelConfig, "embed")
        assert main is not None and embed is not None
        main.base_url = "https://models.example/v1"
        main.model_name = "chat-model"
        main.secret_ref = protect_api_key("sk-main-test")
        embed.base_url = "https://models.example/v1"
        embed.model_name = "embed-model"
        embed.secret_ref = protect_api_key("sk-embed-test")
        embed.vector_dimension = 3
        embed.config_version = 2
        session.commit()


def test_worldbook_api_completes_crud_and_vector_cleanup(tmp_path: Path) -> None:
    """世界书与手工条目 API 完成创建、更新、列表、重建和删除闭环。"""

    client, engine, session_factory, chroma = create_worldbook_client(tmp_path)
    try:
        configure_models(session_factory)
        created = client.post("/api/worldbooks", json={})
        assert created.status_code == 201
        book = created.json()["data"]
        assert book["name"] == "新世界书"
        assert book["entries"] == []

        updated = client.put(
            f"/api/worldbooks/{book['id']}",
            json={"name": "  北境设定集  ", "rawContent": "完整原文"},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["name"] == "北境设定集"

        entry_response = client.post(f"/api/worldbooks/{book['id']}/entries", json={})
        assert entry_response.status_code == 201
        entry = entry_response.json()["data"]
        assert entry["indexStale"] is True

        entry_update = client.put(
            f"/api/worldbooks/{book['id']}/entries/{entry['id']}",
            json={
                "name": "霜门",
                "content": "霜门位于北境边界。",
                "keywords": ["北境", "霜门"],
                "category": "地点",
                "resident": True,
                "enabled": False,
            },
        )
        assert entry_update.status_code == 200
        assert entry_update.json()["data"]["resident"] is True
        assert entry_update.json()["data"]["indexStale"] is True

        reembed = client.post(f"/api/worldbooks/{book['id']}/reembed", json={})
        assert reembed.status_code == 200
        assert reembed.json()["data"] == {"count": 1}
        assert chroma.upserts[0]["metadatas"][0]["worldBookId"] == book["id"]

        listed = client.get("/api/worldbooks")
        assert listed.status_code == 200
        assert listed.json()["data"][0]["entries"][0]["indexStale"] is False

        deleted_entry = client.delete(
            f"/api/worldbooks/{book['id']}/entries/{entry['id']}"
        )
        assert deleted_entry.status_code == 204
        assert chroma.deleted_ids == [worldbook_entry_document_id(entry["id"])]

        deleted_book = client.delete(f"/api/worldbooks/{book['id']}")
        assert deleted_book.status_code == 204
        assert client.get("/api/worldbooks").json()["data"] == []
    finally:
        client.close()
        engine.dispose()


def test_worldbook_split_and_confirm_only_persist_after_confirmation(
    tmp_path: Path,
) -> None:
    """拆分草稿保持临时态，确认后才写入 SQLite 与 Chroma。"""

    client, engine, session_factory, chroma = create_worldbook_client(tmp_path)
    try:
        configure_models(session_factory)
        book = client.post("/api/worldbooks", json={}).json()["data"]
        client.put(
            f"/api/worldbooks/{book['id']}",
            json={"rawContent": "银港被海雾笼罩。"},
        )

        split = client.post(f"/api/worldbooks/{book['id']}/split", json={})
        assert split.status_code == 200
        drafts = split.json()["data"]
        assert drafts == [
            {"name": "银港", "category": "地点", "content": "银港被海雾笼罩。"}
        ]
        assert client.get("/api/worldbooks").json()["data"][0]["entries"] == []
        assert chroma.upserts == []

        confirmed = client.post(
            f"/api/worldbooks/{book['id']}/entries/confirm",
            json={"drafts": drafts},
        )
        assert confirmed.status_code == 201
        entries = confirmed.json()["data"]
        assert len(entries) == 1
        assert entries[0]["indexStale"] is False
        assert chroma.upserts[0]["ids"] == [
            worldbook_entry_document_id(entries[0]["id"])
        ]
    finally:
        client.close()
        engine.dispose()


def test_worldbook_api_returns_validation_and_not_found_errors(tmp_path: Path) -> None:
    """世界书 API 对空更新、空原文和越界资源返回稳定错误。"""

    client, engine, _session_factory, _chroma = create_worldbook_client(tmp_path)
    try:
        book = client.post("/api/worldbooks", json={}).json()["data"]
        invalid = client.put(f"/api/worldbooks/{book['id']}", json={})
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "VALIDATION_ERROR"

        empty_split = client.post(f"/api/worldbooks/{book['id']}/split", json={})
        assert empty_split.status_code == 400
        assert empty_split.json()["code"] == "EMPTY_SOURCE_CONTENT"

        missing = client.post("/api/worldbooks/missing/entries", json={})
        assert missing.status_code == 404
        assert missing.json()["code"] == "RESOURCE_NOT_FOUND"
    finally:
        client.close()
        engine.dispose()
