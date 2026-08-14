"""阶段 9.5 核心用户流程端到端测试。"""

import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from app.api.system import get_chroma_repository, get_model_gateway
from app.core.config import Settings
from app.db.database import (
    create_database_engine,
    create_session_factory,
    get_db_session,
)
from app.main import create_app
from fastapi.testclient import TestClient

from scripts.init_db import initialize_database


class E2EModelGateway:
    """覆盖配置测试、对话、记忆提取与 Embedding 的确定性模型替身。"""

    async def test_main(self, **_kwargs) -> None:
        """主模型连接测试固定成功。"""

    async def test_embedding(self, **_kwargs) -> int:
        """Embedding 连接测试固定返回三维向量。"""

        return 3

    async def create_embeddings(self, **kwargs) -> list[list[float]]:
        """为检索和记忆同步返回数量一致的三维向量。"""

        return [[0.1, 0.2, 0.3] for _text in kwargs["texts"]]

    async def create_chat_completion(self, **kwargs) -> str:
        """根据系统任务返回对话、记忆提取或整合结果。"""

        system_prompt = kwargs["messages"][0]["content"]
        if "当前任务：长期记忆提取" in system_prompt:
            return (
                '{"candidates":[{"type":"长期目标",'
                '"content":"用户希望与伊瑟共同修复星港。",'
                '"importance":5,"sourceRounds":[1]}]}'
            )
        if "当前任务：长期记忆整合" in system_prompt:
            return (
                '{"action":"create",'
                '"content":"用户希望与伊瑟共同修复星港。","importance":5}'
            )
        return (
            '{"blocks":['
            '{"type":"action","content":"她点亮了控制台。"},'
            '{"type":"dialogue","content":"我们继续。"}'
            "]}"
        )


class E2EChroma:
    """端到端流程所需的最小 Chroma 调用面。"""

    def __init__(self) -> None:
        self.memory_upserts: list[str] = []

    def query_worldbook(self, **_kwargs) -> dict[str, object]:
        """空条目世界书不返回向量命中。"""

        return {"ids": [[]], "distances": [[]], "metadatas": [[]]}

    def query_memories(self, **_kwargs) -> dict[str, object]:
        """首批长期记忆生成前没有相似历史。"""

        return {"ids": [[]], "distances": [[]], "metadatas": [[]]}

    def upsert_worldbook(self, **_kwargs) -> None:
        """空条目世界书不会写入快照向量。"""

    def upsert_memories(self, **kwargs) -> None:
        """记录长期记忆已同步到向量层。"""

        self.memory_upserts.extend(kwargs["ids"])

    def delete_worldbook(self, *, ids: list[str]) -> None:
        """端到端流程不删除世界书向量。"""

        assert ids == []

    def delete_memories(self, *, ids: list[str]) -> None:
        """端到端流程不删除长期记忆向量。"""

        assert ids == []


def test_core_user_flow_from_model_setup_to_memory_and_exports(tmp_path: Path) -> None:
    """贯通双 API、内容、十轮对话、记忆、归档和两类导出。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    gateway = E2EModelGateway()
    chroma = E2EChroma()
    application = create_app(settings)

    def override_session():
        """为每个真实 API 请求提供隔离 SQLite Session。"""

        with session_factory() as session:
            yield session

    application.dependency_overrides[get_db_session] = override_session
    application.dependency_overrides[get_model_gateway] = lambda: gateway
    application.dependency_overrides[get_chroma_repository] = lambda: chroma
    main_secret = "sk-e2e-main-secret"
    embed_secret = "sk-e2e-embed-secret"

    try:
        with TestClient(application) as client:
            for group, model, secret in (
                ("main", "chat-model", main_secret),
                ("embed", "embed-model", embed_secret),
            ):
                payload = {
                    "baseUrl": "https://models.example/v1",
                    "model": model,
                    "apiKey": secret,
                }
                assert (
                    client.post(
                        f"/api/settings/models/{group}/test", json=payload
                    ).status_code
                    == 200
                )
                assert (
                    client.put(
                        f"/api/settings/models/{group}", json=payload
                    ).status_code
                    == 200
                )

            public_settings = client.get("/api/settings/models")
            assert public_settings.status_code == 200
            assert "apiKey" not in public_settings.text
            assert main_secret not in public_settings.text
            assert embed_secret not in public_settings.text

            identity = client.put(
                "/api/identity",
                json={
                    "name": "旅人",
                    "personaName": "星港修复师",
                    "bio": "擅长修复旧式机械。",
                },
            ).json()["data"]
            character = client.post("/api/characters", json={}).json()["data"]
            character = client.put(
                f"/api/characters/{character['id']}",
                json={
                    "name": "伊瑟",
                    "introduction": "星港守门人",
                    "systemPrompt": "始终以冷静克制的语气回应。",
                    "dialogueExamples": [
                        {"user": "准备好了吗？", "assistant": "随时可以。"}
                    ],
                },
            ).json()["data"]
            world_book = client.post("/api/worldbooks", json={}).json()["data"]
            world_book = client.put(
                f"/api/worldbooks/{world_book['id']}",
                json={"name": "星港设定", "rawContent": "星港由旧式机械核心驱动。"},
            ).json()["data"]

            created = client.post(
                "/api/sessions",
                json={"characterId": character["id"], "worldBookId": world_book["id"]},
            )
            assert created.status_code == 201
            chat_session = created.json()["data"]
            assert chat_session["identityName"] == identity["name"]
            assert chat_session["characterName"] == "伊瑟"
            assert chat_session["worldBookName"] == "星港设定"

            for round_number in range(1, 11):
                response = client.post(
                    f"/api/sessions/{chat_session['id']}/messages",
                    json={"content": f"继续修复第 {round_number} 个模块。"},
                    headers={"Accept": "text/event-stream"},
                )
                assert response.status_code == 200
                events = [
                    json.loads(frame.removeprefix("data: "))
                    for frame in response.text.strip().split("\n\n")
                ]
                assert events[-1] == {
                    "type": "done",
                    "messageId": events[-1]["messageId"],
                    "roundCount": round_number,
                }

            memories = client.get(
                f"/api/memories?characterId={character['id']}&status=有效"
            ).json()["data"]
            assert len(memories) == 1
            assert memories[0]["type"] == "长期目标"
            assert memories[0]["content"] == "用户希望与伊瑟共同修复星港。"
            assert chroma.memory_upserts

            archived = client.get("/api/sessions").json()["data"]
            assert archived[0]["id"] == chat_session["id"]
            assert archived[0]["roundCount"] == 10
            assert archived[0]["consolidatedRound"] == 10

            history = client.get(f"/api/sessions/{chat_session['id']}/messages").json()[
                "data"
            ]
            assert [block["type"] for block in history[-1]["blocks"]] == [
                "action",
                "dialogue",
            ]

            session_export = client.get(f"/api/export/sessions/{chat_session['id']}")
            assert session_export.status_code == 200
            exported_session = session_export.json()
            assert exported_session["session"]["roundCount"] == 10
            assert (
                exported_session["messages"][-1]["blocks"][1]["content"] == "我们继续。"
            )

            full_export = client.get("/api/export/all")
            assert full_export.status_code == 200
            with ZipFile(BytesIO(full_export.content)) as archive:
                assert set(archive.namelist()) == {
                    "manifest.json",
                    "identity.json",
                    "characters.json",
                    "worldbooks.json",
                    "sessions.json",
                    "messages.json",
                    "memories.json",
                }
                raw_export = b"".join(archive.read(name) for name in archive.namelist())
            assert main_secret.encode("utf-8") not in raw_export
            assert embed_secret.encode("utf-8") not in raw_export
    finally:
        engine.dispose()
