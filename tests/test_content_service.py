"""阶段 3 用户身份与角色卡内容服务单元测试。"""

from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.exceptions import AppError
from app.db.database import create_database_engine, create_session_factory
from app.db.models import ChatSession
from app.schemas.dto import UpdateCharacterPayload, UpdateUserIdentityPayload
from app.services.content_service import ContentService

from scripts.init_db import initialize_database


def create_service(tmp_path: Path):
    """创建使用隔离数据库的内容服务与 Session。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session = create_session_factory(engine)()
    return ContentService(session), session, engine


def test_identity_partial_update_is_persisted(tmp_path: Path) -> None:
    """身份只更新提交字段，并在新 Session 中仍可读取。"""

    service, session, engine = create_service(tmp_path)
    try:
        original = service.get_identity()
        saved = service.update_identity(
            UpdateUserIdentityPayload(name="  林舟  ", bio="来自远方的记录者")
        )
        assert saved.name == "林舟"
        assert saved.persona_name == original.persona_name
        assert saved.bio == "来自远方的记录者"

        session.close()
        with create_session_factory(engine)() as reopened_session:
            persisted = ContentService(reopened_session).get_identity()
            assert persisted == saved
    finally:
        session.close()
        engine.dispose()


def test_character_crud_preserves_defaults_and_dialogue_order(tmp_path: Path) -> None:
    """角色卡默认值和有序对话示例可以完整写回。"""

    service, session, engine = create_service(tmp_path)
    try:
        created = service.create_character()
        assert created.name == "新角色"
        assert created.introduction == "一句话介绍"
        assert created.dialogue_examples == []

        updated = service.update_character(
            created.id,
            UpdateCharacterPayload(
                name="归舟",
                system_prompt="始终使用简短、克制的语气。",
                dialogue_examples=[
                    {"user": "第一问", "assistant": "第一答"},
                    {"user": "第二问", "assistant": "第二答"},
                ],
            ),
        )
        assert updated.name == "归舟"
        assert [example.user for example in updated.dialogue_examples] == [
            "第一问",
            "第二问",
        ]
        assert service.list_characters() == [updated]

        service.delete_character(created.id)
        assert service.list_characters() == []
    finally:
        session.close()
        engine.dispose()


def test_character_delete_rejects_historical_session_reference(tmp_path: Path) -> None:
    """历史会话引用存在时返回 409，且角色卡仍然保留。"""

    service, session, engine = create_service(tmp_path)
    try:
        character = service.create_character()
        session.add(ChatSession(title="历史会话", character_id=character.id))
        session.commit()

        with pytest.raises(AppError) as error:
            service.delete_character(character.id)

        assert error.value.status_code == 409
        assert error.value.code == "RESOURCE_IN_USE"
        assert service.list_characters() == [character]
    finally:
        session.close()
        engine.dispose()


def test_missing_character_uses_stable_not_found_error(tmp_path: Path) -> None:
    """更新或删除不存在的角色卡时返回统一 404 业务错误。"""

    service, session, engine = create_service(tmp_path)
    try:
        with pytest.raises(AppError) as error:
            service.update_character(
                "missing-character",
                UpdateCharacterPayload(name="不存在"),
            )

        assert error.value.status_code == 404
        assert error.value.code == "RESOURCE_NOT_FOUND"
    finally:
        session.close()
        engine.dispose()
