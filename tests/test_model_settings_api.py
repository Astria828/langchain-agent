"""阶段 2A 模型配置真实接口集成测试。"""

from pathlib import Path

from app.api.system import get_model_gateway
from app.core.config import Settings
from app.core.exceptions import AppError
from app.db.database import (
    create_database_engine,
    create_session_factory,
    get_db_session,
)
from app.db.models import ModelConfig, WorldBook, WorldBookEntry
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from scripts.init_db import initialize_database


class FakeModelGateway:
    """接口测试使用的可控模型服务替身。"""

    def __init__(self) -> None:
        self.embedding_dimension = 3
        self.main_error: AppError | None = None
        self.main_calls: list[dict[str, str]] = []
        self.embedding_calls: list[dict[str, str]] = []

    async def test_main(self, *, base_url: str, model: str, api_key: str) -> None:
        self.main_calls.append(
            {"base_url": base_url, "model": model, "api_key": api_key}
        )
        if self.main_error is not None:
            raise self.main_error

    async def test_embedding(self, *, base_url: str, model: str, api_key: str) -> int:
        self.embedding_calls.append(
            {"base_url": base_url, "model": model, "api_key": api_key}
        )
        return self.embedding_dimension


def create_api_client(
    tmp_path: Path,
    gateway: FakeModelGateway,
) -> tuple[TestClient, Engine, sessionmaker[Session]]:
    """创建使用隔离数据库和可控模型网关的真实 FastAPI 客户端。"""

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
    return TestClient(app), engine, session_factory


def model_payload(
    *,
    base_url: str = "https://models.example/v1",
    model: str = "chat-model",
    api_key: str = "sk-api-test-12345678",
) -> dict[str, str]:
    """构造模型接口请求体。"""

    return {"baseUrl": base_url, "model": model, "apiKey": api_key}


def test_get_model_settings_returns_two_empty_safe_configs(tmp_path: Path) -> None:
    """初始查询返回两组配置且不包含 apiKey 字段。"""

    client, engine, _session_factory = create_api_client(tmp_path, FakeModelGateway())
    try:
        response = client.get("/api/settings/models")
        assert response.status_code == 200
        payload = response.json()
        assert payload["data"] == {
            "main": {"baseUrl": "", "model": "", "keySet": False, "keyTail": ""},
            "embed": {"baseUrl": "", "model": "", "keySet": False, "keyTail": ""},
        }
        assert "apiKey" not in response.text
        assert payload["requestId"] == response.headers["X-Request-ID"]
    finally:
        client.close()
        engine.dispose()


def test_configuration_cannot_be_saved_before_matching_test(tmp_path: Path) -> None:
    """未经当前参数真实测试的配置返回稳定 409。"""

    client, engine, _session_factory = create_api_client(tmp_path, FakeModelGateway())
    try:
        response = client.put("/api/settings/models/main", json=model_payload())
        assert response.status_code == 409
        assert response.json()["code"] == "MODEL_CONFIG_NOT_TESTED"
    finally:
        client.close()
        engine.dispose()


def test_main_configuration_test_save_and_reuse_saved_key(tmp_path: Path) -> None:
    """测试成功后可保存，后续空 apiKey 可复用 DPAPI 密钥。"""

    gateway = FakeModelGateway()
    client, engine, _session_factory = create_api_client(tmp_path, gateway)
    secret = "sk-main-secret-12345678"
    payload = model_payload(api_key=secret)
    try:
        test_response = client.post("/api/settings/models/main/test", json=payload)
        assert test_response.status_code == 200
        assert test_response.json()["data"]["ok"] is True

        save_response = client.put("/api/settings/models/main", json=payload)
        assert save_response.status_code == 200
        public_config = save_response.json()["data"]["main"]
        assert public_config == {
            "baseUrl": "https://models.example/v1",
            "model": "chat-model",
            "keySet": True,
            "keyTail": "5678",
        }
        assert secret not in save_response.text

        reuse_response = client.post(
            "/api/settings/models/main/test",
            json=model_payload(api_key=""),
        )
        assert reuse_response.status_code == 200
        assert gateway.main_calls[-1]["api_key"] == secret
    finally:
        client.close()
        engine.dispose()


def test_embedding_save_records_dimension_version_and_rebuild_state(
    tmp_path: Path,
) -> None:
    """Embedding 首次保存记录维度，后续向量配置变化标记待重建。"""

    gateway = FakeModelGateway()
    client, engine, session_factory = create_api_client(tmp_path, gateway)
    first = model_payload(model="embed-v1")
    changed = model_payload(
        base_url="https://second.example/v1",
        model="embed-v2",
        api_key="sk-embed-secret-87654321",
    )
    try:
        assert (
            client.post("/api/settings/models/embed/test", json=first).status_code
            == 200
        )
        assert client.put("/api/settings/models/embed", json=first).status_code == 200
        assert client.get("/api/index/status").json()["data"] == {
            "rebuildRequired": False
        }

        with session_factory.begin() as session:
            book = WorldBook(name="待重建世界书")
            session.add(book)
            session.flush()
            session.add(
                WorldBookEntry(
                    world_book_id=book.id,
                    name="旧条目",
                    content="旧内容",
                    content_hash="old-hash",
                    index_status="ready",
                    embedding_version=1,
                )
            )

        gateway.embedding_dimension = 4
        assert (
            client.post("/api/settings/models/embed/test", json=changed).status_code
            == 200
        )
        assert client.put("/api/settings/models/embed", json=changed).status_code == 200
        assert client.get("/api/index/status").json()["data"] == {
            "rebuildRequired": True
        }

        with session_factory() as session:
            config = session.scalar(
                select(ModelConfig).where(ModelConfig.group_name == "embed")
            )
            assert config is not None
            assert config.vector_dimension == 4
            assert config.tested_vector_dimension == 4
            assert config.config_version == 2
            assert config.rebuild_required == 1
            assert config.secret_ref is not None
            assert changed["apiKey"] not in config.secret_ref
            entry = session.scalar(select(WorldBookEntry))
            assert entry is not None
            assert entry.index_status == "stale"
            assert entry.last_index_error is None
    finally:
        client.close()
        engine.dispose()


def test_connection_failure_is_sanitized_and_does_not_enable_save(
    tmp_path: Path,
) -> None:
    """上游失败直接返回脱敏错误，不能通过失败参数保存配置。"""

    gateway = FakeModelGateway()
    gateway.main_error = AppError(
        status_code=502,
        code="MODEL_CONNECTION_FAILED",
        message="无法连接模型服务",
    )
    client, engine, _session_factory = create_api_client(tmp_path, gateway)
    secret = "sk-never-return-this"
    payload = model_payload(api_key=secret)
    try:
        test_response = client.post("/api/settings/models/main/test", json=payload)
        assert test_response.status_code == 502
        assert test_response.json()["code"] == "MODEL_CONNECTION_FAILED"
        assert secret not in test_response.text

        save_response = client.put("/api/settings/models/main", json=payload)
        assert save_response.status_code == 409
        assert save_response.json()["code"] == "MODEL_CONFIG_NOT_TESTED"
    finally:
        client.close()
        engine.dispose()


def test_missing_key_and_invalid_group_return_validation_errors(tmp_path: Path) -> None:
    """未保存密钥和非法 group 都不会触发外部模型调用。"""

    gateway = FakeModelGateway()
    client, engine, _session_factory = create_api_client(tmp_path, gateway)
    try:
        missing_key = client.post(
            "/api/settings/models/main/test",
            json=model_payload(api_key=""),
        )
        assert missing_key.status_code == 422
        assert missing_key.json()["code"] == "VALIDATION_ERROR"

        invalid_group = client.post(
            "/api/settings/models/other/test",
            json=model_payload(),
        )
        assert invalid_group.status_code == 422
        assert gateway.main_calls == []
    finally:
        client.close()
        engine.dispose()
