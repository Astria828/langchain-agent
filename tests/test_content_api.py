"""阶段 3 用户身份与角色卡 API 集成测试。"""

from pathlib import Path

from app.core.config import Settings
from app.db.database import (
    create_database_engine,
    create_session_factory,
    get_db_session,
)
from app.db.models import ChatSession
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from scripts.init_db import initialize_database


def create_content_client(
    tmp_path: Path,
) -> tuple[TestClient, Engine, sessionmaker[Session]]:
    """创建使用隔离数据库的内容 API 客户端。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    app = create_app(settings)

    def override_db_session():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db_session
    return TestClient(app), engine, session_factory


def test_identity_api_reads_and_partially_updates_current_identity(
    tmp_path: Path,
) -> None:
    """身份接口返回默认记录，并只修改请求中提交的字段。"""

    client, engine, _session_factory = create_content_client(tmp_path)
    try:
        initial_response = client.get("/api/identity")
        assert initial_response.status_code == 200
        assert initial_response.json()["data"] == {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "用户",
            "personaName": "默认身份",
            "bio": "",
        }
        assert (
            initial_response.json()["requestId"]
            == initial_response.headers["X-Request-ID"]
        )

        update_response = client.put(
            "/api/identity",
            json={"name": "  林舟  ", "bio": "来自远方的记录者"},
        )
        assert update_response.status_code == 200
        assert update_response.json()["data"] == {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "林舟",
            "personaName": "默认身份",
            "bio": "来自远方的记录者",
        }

        persisted_response = client.get("/api/identity")
        assert persisted_response.json()["data"] == update_response.json()["data"]
    finally:
        client.close()
        engine.dispose()


def test_identity_api_rejects_invalid_partial_updates(tmp_path: Path) -> None:
    """身份更新拒绝空请求、null、空名称和 id 字段。"""

    client, engine, _session_factory = create_content_client(tmp_path)
    try:
        for payload in ({}, {"bio": None}, {"name": "  "}, {"id": "identity-2"}):
            response = client.put("/api/identity", json=payload)
            assert response.status_code == 422
            assert response.json()["code"] == "VALIDATION_ERROR"
            assert response.json()["requestId"] == response.headers["X-Request-ID"]
    finally:
        client.close()
        engine.dispose()


def test_character_api_completes_crud_with_contract_fields(tmp_path: Path) -> None:
    """角色卡接口按契约默认值创建，并保持对话示例顺序。"""

    client, engine, _session_factory = create_content_client(tmp_path)
    try:
        create_response = client.post("/api/characters", json={})
        assert create_response.status_code == 201
        created = create_response.json()["data"]
        assert created == {
            "id": created["id"],
            "name": "新角色",
            "introduction": "一句话介绍",
            "systemPrompt": "",
            "dialogueExamples": [],
        }

        update_response = client.put(
            f"/api/characters/{created['id']}",
            json={
                "name": "  归舟  ",
                "systemPrompt": "始终使用简短、克制的语气。",
                "dialogueExamples": [
                    {"user": "第一问", "assistant": "第一答"},
                    {"user": "第二问", "assistant": "第二答"},
                ],
            },
        )
        assert update_response.status_code == 200
        updated = update_response.json()["data"]
        assert updated["name"] == "归舟"
        assert updated["introduction"] == "一句话介绍"
        assert updated["dialogueExamples"] == [
            {"user": "第一问", "assistant": "第一答"},
            {"user": "第二问", "assistant": "第二答"},
        ]

        list_response = client.get("/api/characters")
        assert list_response.status_code == 200
        assert list_response.json()["data"] == [updated]

        delete_response = client.delete(f"/api/characters/{created['id']}")
        assert delete_response.status_code == 204
        assert delete_response.content == b""
        assert client.get("/api/characters").json()["data"] == []
    finally:
        client.close()
        engine.dispose()


def test_character_api_returns_validation_not_found_and_conflict_errors(
    tmp_path: Path,
) -> None:
    """角色卡接口稳定返回 422、404 和历史引用冲突。"""

    client, engine, session_factory = create_content_client(tmp_path)
    try:
        created = client.post("/api/characters", json={}).json()["data"]

        for payload in ({}, {"name": "  "}, {"systemPrompt": None}, {"id": "other"}):
            response = client.put(f"/api/characters/{created['id']}", json=payload)
            assert response.status_code == 422
            assert response.json()["code"] == "VALIDATION_ERROR"

        missing_response = client.put(
            "/api/characters/missing-character",
            json={"name": "不存在"},
        )
        assert missing_response.status_code == 404
        assert missing_response.json()["code"] == "RESOURCE_NOT_FOUND"

        with session_factory() as session:
            session.add(ChatSession(title="历史会话", character_id=created["id"]))
            session.commit()

        conflict_response = client.delete(f"/api/characters/{created['id']}")
        assert conflict_response.status_code == 409
        assert conflict_response.json()["code"] == "RESOURCE_IN_USE"
        assert client.get("/api/characters").json()["data"] == [created]
    finally:
        client.close()
        engine.dispose()
