"""阶段 8.3–8.4 单会话 JSON 与全量业务 ZIP 导出测试。"""

import json
import os
from pathlib import Path
from zipfile import ZipFile

import pytest
from app.core.config import Settings
from app.core.exceptions import AppError
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
    MemorySource,
    ModelConfig,
    UserIdentity,
    WorldBook,
    WorldBookEntry,
)
from app.db.models import Message as MessageModel
from app.db.models import MessageBlock as MessageBlockModel
from app.gateways.model_gateway import ModelGateway
from app.main import create_app
from app.services.system_service import FULL_EXPORT_FILE_NAMES, SystemService
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from scripts.init_db import initialize_database


def create_export_service(
    tmp_path: Path,
) -> tuple[SystemService, Session, object, Settings]:
    """创建包含全部业务实体、敏感排除项和不可变快照的隔离数据库。"""

    settings = Settings(
        environment="test",
        app_version="9.9.9",
        data_dir=tmp_path,
    )
    initialize_database(settings)
    engine = create_database_engine(settings)
    session = create_session_factory(engine)()

    identity = session.scalar(
        select(UserIdentity).where(UserIdentity.singleton_key == "current")
    )
    assert identity is not None
    identity.name = "当前身份"
    identity.persona_name = "当前身份名称"
    identity.bio = "当前用户设定"

    character = Character(
        id="character-1",
        name="当前角色",
        introduction="当前简介",
        system_prompt="当前系统提示词",
        dialogue_examples_json=json.dumps(
            [{"user": "现在好吗", "assistant": "很好"}],
            ensure_ascii=False,
        ),
    )
    world_book = WorldBook(
        id="worldbook-1",
        name="当前世界书",
        raw_content="当前世界书原文",
    )
    session.add_all([character, world_book])
    session.flush()

    world_book_entries = [
        WorldBookEntry(
            id="entry-2",
            world_book_id=world_book.id,
            position=1,
            name="第二条目",
            content="第二条目正文",
            keywords_json='["第二"]',
            category="地点",
            resident=0,
            enabled=1,
            index_status="ready",
            embedding_version=1,
            content_hash="entry-hash-2",
        ),
        WorldBookEntry(
            id="entry-1",
            world_book_id=world_book.id,
            position=0,
            name="第一条目",
            content="第一条目正文",
            keywords_json='["第一"]',
            category="规则",
            resident=1,
            enabled=1,
            index_status="ready",
            embedding_version=1,
            content_hash="entry-hash-1",
        ),
    ]
    chat_session = ChatSession(
        id="session-1",
        title="旧世界的会话",
        character_id=character.id,
        world_book_id=world_book.id,
        round_count=1,
        consolidated_round=0,
        summary="已完成一轮对话。",
    )
    session.add_all([*world_book_entries, chat_session])
    session.flush()

    messages = [
        MessageModel(
            id="message-2",
            session_id=chat_session.id,
            position=1,
            role="assistant",
            retrieved_entries_json='["历史第一条目"]',
        ),
        MessageModel(
            id="message-1",
            session_id=chat_session.id,
            position=0,
            role="user",
        ),
    ]
    memory = LongTermMemory(
        id="memory-1",
        character_id=character.id,
        type="重要剧情",
        content="用户与角色完成了第一次会面。",
        importance=4,
        status="active",
        source_label="旧世界的会话",
        memory_version=1,
        index_status="ready",
        embedding_version=1,
        content_hash="memory-hash-1",
    )
    session.add_all([*messages, memory])
    session.flush()

    session.add_all(
        [
            MessageBlockModel(
                id="block-2-1",
                message_id="message-2",
                sequence=1,
                type="dialogue",
                content="欢迎回来。",
            ),
            MessageBlockModel(
                id="block-2-0",
                message_id="message-2",
                sequence=0,
                type="action",
                content="她抬起头。",
            ),
            MessageBlockModel(
                id="block-1-0",
                message_id="message-1",
                sequence=0,
                type="dialogue",
                content="我回来了。",
            ),
            MemorySource(
                memory_id=memory.id,
                message_id="message-2",
                source_order=0,
            ),
            BackgroundTask(
                id="task-1",
                task_type="vector_cleanup",
                status="pending",
                scope_id="forbidden-background-task",
                progress_total=1,
            ),
        ]
    )
    model_config = session.get(ModelConfig, "main")
    assert model_config is not None
    model_config.secret_ref = "forbidden-api-key-secret"
    session.commit()

    settings.logs_dir.joinpath("loreweave.log").write_text(
        "forbidden-log-content",
        encoding="utf-8",
    )
    settings.chroma_dir.joinpath("forbidden-chroma-content.bin").write_bytes(b"chroma")
    return SystemService(session, ModelGateway(), settings), session, engine, settings


def business_state(session: Session) -> dict[str, object]:
    """读取导出前后必须保持不变的业务状态。"""

    chat_session = session.get(ChatSession, "session-1")
    task = session.get(BackgroundTask, "task-1")
    assert chat_session is not None and task is not None
    return {
        "roundCount": chat_session.round_count,
        "consolidatedRound": chat_session.consolidated_round,
        "taskStatus": task.status,
        "messages": session.scalar(select(func.count(MessageModel.id))),
        "memories": session.scalar(select(func.count(LongTermMemory.id))),
    }


def test_session_export_uses_live_bindings_and_stable_message_order(
    tmp_path: Path,
) -> None:
    """单会话 JSON 使用导出时的实时绑定，保留中文、消息位置和块序号且不写业务库。"""

    service, session, engine, settings = create_export_service(tmp_path)
    try:
        unbound_session = ChatSession(
            id="session-unbound",
            title="无世界书会话",
            character_id="character-1",
            world_book_id=None,
            round_count=0,
            consolidated_round=0,
            summary="未绑定世界书。",
        )
        session.add(unbound_session)
        session.flush()
        session.commit()

        before = business_state(session)
        exported = service.export_session_json("session-1")
        payload = json.loads(exported.decode("utf-8"))

        assert exported.startswith(b"{")
        assert payload["schemaVersion"] == 2
        assert payload["session"]["identityName"] == "当前身份"
        assert payload["session"]["characterName"] == "当前角色"
        assert payload["session"]["worldBookName"] == "当前世界书"
        assert "contextSnapshot" not in payload
        assert "worldBookEntrySnapshots" not in payload
        assert [message["position"] for message in payload["messages"]] == [0, 1]
        assert [block["sequence"] for block in payload["messages"][1]["blocks"]] == [
            0,
            1,
        ]
        assert payload["messages"][1]["blocks"][0]["type"] == "action"
        assert business_state(session) == before
        assert list(settings.exports_dir.iterdir()) == []

        unbound_payload = json.loads(
            service.export_session_json("session-unbound").decode("utf-8")
        )
        assert unbound_payload["session"]["worldBookId"] is None
        assert unbound_payload["session"]["worldBookName"] is None

        with pytest.raises(AppError) as exc_info:
            service.export_session_json("missing-session")
        assert exc_info.value.status_code == 404
        assert exc_info.value.message == "会话不存在"
    finally:
        session.close()
        engine.dispose()


def test_full_export_contains_business_files_and_excludes_runtime_secrets(
    tmp_path: Path,
) -> None:
    """全量 ZIP 包含完整业务关系，不包含密钥、日志、任务、Chroma 或旧导出。"""

    service, session, engine, settings = create_export_service(tmp_path)
    outside = settings.resolved_data_dir / "export-outside.zip"
    try:
        expired = settings.exports_dir / "export-expired.zip"
        expired.write_bytes(b"expired")
        os.utime(expired, (0, 0))
        unmanaged = settings.exports_dir / "keep.zip"
        unmanaged.write_bytes(b"keep")
        os.utime(unmanaged, (0, 0))
        outside.write_bytes(b"outside")

        before = business_state(session)
        log_before = settings.logs_dir.joinpath("loreweave.log").read_bytes()
        chroma_before = settings.chroma_dir.joinpath(
            "forbidden-chroma-content.bin"
        ).read_bytes()
        export_path = service.create_full_export()

        assert export_path.parent == settings.exports_dir.resolve()
        assert export_path.name.startswith("export-")
        assert not expired.exists()
        assert unmanaged.exists()
        with ZipFile(export_path) as archive:
            assert archive.namelist() == list(FULL_EXPORT_FILE_NAMES)
            decoded = {
                name: json.loads(archive.read(name).decode("utf-8"))
                for name in FULL_EXPORT_FILE_NAMES
            }
            raw_archive = b"".join(archive.read(name) for name in archive.namelist())

        manifest = decoded["manifest.json"]
        assert manifest["appVersion"] == "9.9.9"
        assert manifest["alembicRevision"] == session.scalar(
            text("SELECT version_num FROM alembic_version")
        )
        assert manifest["counts"] == {
            "identities": 1,
            "characters": 1,
            "worldBooks": 1,
            "worldBookEntries": 2,
            "sessions": 1,
            "messages": 2,
            "messageBlocks": 3,
            "memories": 1,
            "memorySources": 1,
        }
        assert decoded["identity.json"]["name"] == "当前身份"
        assert (
            decoded["characters.json"][0]["dialogueExamples"][0]["user"] == "现在好吗"
        )
        assert [
            entry["position"] for entry in decoded["worldbooks.json"][0]["entries"]
        ] == [0, 1]
        assert (
            decoded["sessions.json"][0]["identityName"] == "当前身份"
        )
        assert [message["position"] for message in decoded["messages.json"]] == [0, 1]
        assert decoded["memories.json"][0]["sources"] == [
            {"messageId": "message-2", "sourceOrder": 0}
        ]

        for forbidden in (
            b"forbidden-api-key-secret",
            b"forbidden-background-task",
            b"forbidden-log-content",
            b"forbidden-chroma-content",
        ):
            assert forbidden not in raw_archive
        assert business_state(session) == before
        assert settings.logs_dir.joinpath("loreweave.log").read_bytes() == log_before
        assert (
            settings.chroma_dir.joinpath("forbidden-chroma-content.bin").read_bytes()
            == chroma_before
        )

        service.delete_export_file(export_path)
        assert not export_path.exists()
        with pytest.raises(ValueError, match="不属于受管临时目录"):
            service.delete_export_file(outside)
        assert outside.exists()
    finally:
        session.close()
        engine.dispose()


def test_export_api_downloads_files_and_reclaims_full_export(tmp_path: Path) -> None:
    """真实导出路由返回约定文件名，并在全量下载结束后删除临时 ZIP。"""

    _, session, engine, settings = create_export_service(tmp_path)
    task = session.get(BackgroundTask, "task-1")
    assert task is not None
    task.status = "succeeded"
    session.commit()
    session_factory = create_session_factory(engine)
    application = create_app(settings)

    def override_session():
        """为每个导出请求提供隔离数据库 Session。"""

        with session_factory() as request_session:
            yield request_session

    application.dependency_overrides[get_db_session] = override_session
    try:
        with TestClient(application) as client:
            session_response = client.get("/api/export/sessions/session-1")
            assert session_response.status_code == 200
            assert (
                session_response.headers["content-type"]
                == "application/json; charset=utf-8"
            )
            assert (
                "loreweave-session-session-1.json"
                in session_response.headers["content-disposition"]
            )
            assert session_response.json()["session"]["characterName"] == "当前角色"

            missing_response = client.get("/api/export/sessions/missing-session")
            assert missing_response.status_code == 404
            assert missing_response.json()["code"] == "RESOURCE_NOT_FOUND"

            full_response = client.get("/api/export/all")
            assert full_response.status_code == 200
            assert full_response.headers["content-type"] == "application/zip"
            assert "loreweave-export-" in full_response.headers["content-disposition"]
            archive_path = tmp_path / "downloaded-export.zip"
            archive_path.write_bytes(full_response.content)
            with ZipFile(archive_path) as archive:
                assert archive.namelist() == list(FULL_EXPORT_FILE_NAMES)

        assert list(settings.exports_dir.glob("export-*.zip")) == []
        log_text = settings.logs_dir.joinpath("loreweave.log").read_text(
            encoding="utf-8"
        )
        for forbidden_business_content in (
            "当前系统提示词",
            "当前世界书原文",
            "欢迎回来。",
            "用户与角色完成了第一次会面。",
        ):
            assert forbidden_business_content not in log_text
    finally:
        session.close()
        engine.dispose()
