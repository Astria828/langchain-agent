"""用户身份与角色卡的持久化业务规则。"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.db.models import Character as CharacterModel
from app.db.models import ChatSession, utc_now
from app.db.models import UserIdentity as UserIdentityModel
from app.repositories.sqlite_repository import SQLiteRepository
from app.schemas.dto import (
    Character,
    UpdateCharacterPayload,
    UpdateUserIdentityPayload,
    UserIdentity,
)


class ContentService:
    """管理单用户身份与角色卡，并在服务层控制事务提交。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = SQLiteRepository(session)

    def get_identity(self) -> UserIdentity:
        """读取迁移保证存在的唯一当前身份。"""

        return self._identity_to_dto(self._get_identity_model())

    def update_identity(self, payload: UpdateUserIdentityPayload) -> UserIdentity:
        """仅更新请求中实际提交的身份字段。"""

        identity = self._get_identity_model()
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(identity, field, value)
        identity.updated_at = utc_now()
        self.session.commit()
        return self._identity_to_dto(identity)

    def list_characters(self) -> list[Character]:
        """按最近更新时间倒序返回全部角色卡。"""

        characters = self.repository.list_all(
            CharacterModel,
            order_by=(CharacterModel.updated_at.desc(), CharacterModel.id),
        )
        return [self._character_to_dto(character) for character in characters]

    def create_character(self) -> Character:
        """按 API 契约中的固定默认值创建角色卡。"""

        character = CharacterModel(
            name="新角色",
            introduction="一句话介绍",
            system_prompt="",
            dialogue_examples_json="[]",
        )
        self.repository.add(character)
        self.session.commit()
        return self._character_to_dto(character)

    def update_character(
        self,
        character_id: str,
        payload: UpdateCharacterPayload,
    ) -> Character:
        """更新角色卡字段，并保持对话示例的提交顺序。"""

        character = self._get_character_model(character_id)
        changes = payload.model_dump(exclude_unset=True)
        dialogue_examples = changes.pop("dialogue_examples", None)
        for field, value in changes.items():
            setattr(character, field, value)
        if dialogue_examples is not None:
            character.dialogue_examples_json = json.dumps(
                dialogue_examples,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        character.updated_at = utc_now()
        self.session.commit()
        return self._character_to_dto(character)

    def delete_character(self, character_id: str) -> None:
        """仅删除未被历史会话引用的角色卡。"""

        character = self._get_character_model(character_id)
        referenced_session_id = self.session.scalar(
            select(ChatSession.id).where(ChatSession.character_id == character_id).limit(1)
        )
        if referenced_session_id is not None:
            raise AppError(
                status_code=409,
                code="RESOURCE_IN_USE",
                message="角色卡已被历史会话引用，不能删除",
            )
        self.repository.delete(character)
        self.session.commit()

    def _get_identity_model(self) -> UserIdentityModel:
        """按单例键读取当前身份；缺失表示数据库初始化不完整。"""

        identity = self.session.scalar(
            select(UserIdentityModel).where(UserIdentityModel.singleton_key == "current")
        )
        if identity is None:
            raise RuntimeError("缺少当前用户身份记录")
        return identity

    def _get_character_model(self, character_id: str) -> CharacterModel:
        """读取角色卡，并将不存在转换为稳定业务错误。"""

        character = self.repository.get(CharacterModel, character_id)
        if character is None:
            raise AppError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="角色卡不存在",
            )
        return character

    @staticmethod
    def _identity_to_dto(identity: UserIdentityModel) -> UserIdentity:
        """将身份数据库实体转换为公开 DTO。"""

        return UserIdentity(
            id=identity.id,
            name=identity.name,
            persona_name=identity.persona_name,
            bio=identity.bio,
        )

    @staticmethod
    def _character_to_dto(character: CharacterModel) -> Character:
        """解析并校验角色卡中有序保存的对话示例。"""

        return Character(
            id=character.id,
            name=character.name,
            introduction=character.introduction,
            system_prompt=character.system_prompt,
            dialogue_examples=json.loads(character.dialogue_examples_json),
        )
