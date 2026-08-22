"""模型配置、业务数据导出和系统状态规则。"""

import json
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import time
from typing import Literal
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging import (
    LOG_FILE_NAME,
    pause_application_file_logging,
    sanitize_log_text,
)
from app.core.security import api_key_tail, protect_api_key, unprotect_api_key
from app.db.models import (
    Character,
    ChatSession,
    LongTermMemory,
    MemorySource,
    ModelConfig,
    UserIdentity,
    WorldBook,
    WorldBookEntry,
    utc_now,
)
from app.db.models import Message as MessageModel
from app.db.models import MessageBlock as MessageBlockModel
from app.gateways.model_gateway import ModelGateway, normalize_base_url
from app.repositories.sqlite_repository import SQLiteRepository
from app.schemas.dto import (
    ConnectionTestResult,
    IndexStatus,
    LogEntry,
    LogLevel,
    LogRange,
    ModelEndpointConfig,
    ModelEndpointPayload,
    ModelGroup,
    ModelSettings,
    parse_extra_body,
)
from app.services.chat_service import session_to_dto

logger = logging.getLogger(__name__)

EXPORT_SCHEMA_VERSION = 2
EXPORT_MAX_AGE_SECONDS = 24 * 60 * 60
FULL_EXPORT_FILE_NAMES = (
    "manifest.json",
    "identity.json",
    "characters.json",
    "worldbooks.json",
    "sessions.json",
    "messages.json",
    "memories.json",
)


class SystemService:
    """控制模型配置事务，并从 SQLite 只读组装业务导出。"""

    def __init__(
        self,
        session: Session,
        gateway: ModelGateway,
        settings: Settings,
    ) -> None:
        self.session = session
        self.repository = SQLiteRepository(session)
        self.gateway = gateway
        self.settings = settings

    def get_model_settings(self) -> ModelSettings:
        """返回两组不含明文密钥的当前生效配置。"""

        return ModelSettings(
            main=self._public_config(self._get_config("main")),
            embed=self._public_config(self._get_config("embed")),
        )

    def get_index_status(self) -> IndexStatus:
        """读取 Embedding 配置对应的全局重建标记。"""

        return IndexStatus(rebuild_required=bool(self._get_config("embed").rebuild_required))

    def export_session_json(self, session_id: str) -> bytes:
        """只读导出一个会话的不可变配置、条目快照和有序消息。"""

        chat_session = self.repository.get(ChatSession, session_id)
        if chat_session is None:
            raise AppError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="会话不存在",
            )
        payload = {
            "schemaVersion": EXPORT_SCHEMA_VERSION,
            "exportedAt": utc_now(),
            "session": session_to_dto(
                self.repository,
                chat_session,
                self._require_identity(),
            ).model_dump(by_alias=True),
            "messages": self._serialize_messages(session_id=chat_session.id),
        }
        exported = self._json_bytes(payload)
        logger.info(
            "单会话导出已生成",
            extra={
                "event": "session_export_completed",
                "business_ids": {"sessionId": session_id},
            },
        )
        return exported

    def create_full_export(self) -> Path:
        """把全部业务事实写入受控临时 ZIP，并返回待下载文件路径。"""

        self.cleanup_expired_exports()
        exported_at = utc_now()
        files = self._build_full_export_files(exported_at)
        exports_dir = self.settings.exports_dir.resolve()
        exports_dir.mkdir(parents=True, exist_ok=True)
        target = exports_dir / f"export-{uuid4().hex}.zip"
        try:
            with ZipFile(target, mode="x", compression=ZIP_DEFLATED) as archive:
                for file_name in FULL_EXPORT_FILE_NAMES:
                    archive.writestr(file_name, self._json_bytes(files[file_name]))
        except Exception:
            target.unlink(missing_ok=True)
            logger.exception(
                "全量导出生成失败",
                extra={"event": "full_export_failed", "business_ids": {}},
            )
            raise
        logger.info(
            "全量导出已生成",
            extra={"event": "full_export_completed", "business_ids": {}},
        )
        return target

    def cleanup_expired_exports(
        self,
        *,
        max_age_seconds: int = EXPORT_MAX_AGE_SECONDS,
    ) -> int:
        """只删除导出目录内超过时效的受管 ZIP，不触碰其他文件。"""

        exports_dir = self.settings.exports_dir.resolve()
        exports_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time() - max_age_seconds
        removed = 0
        for candidate in exports_dir.iterdir():
            if not self._is_managed_export(candidate, exports_dir):
                continue
            if candidate.stat().st_mtime >= cutoff:
                continue
            candidate.unlink()
            removed += 1
        return removed

    def delete_export_file(self, path: Path) -> None:
        """下载结束后删除确定的受管临时 ZIP。"""

        exports_dir = self.settings.exports_dir.resolve()
        if not self._is_managed_export(path, exports_dir):
            raise ValueError("导出文件不属于受管临时目录")
        path.unlink(missing_ok=True)

    def list_logs(
        self,
        *,
        level: LogLevel | Literal["all"] = "all",
        range_: LogRange = "all",
    ) -> list[LogEntry]:
        """读取受管日志文件，并按统一级别和时间范围降序筛选。"""

        try:
            now = datetime.now(UTC)
            entries = [
                (timestamp, entry)
                for path in self._managed_log_files()
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(),
                    start=1,
                )
                for timestamp, entry in [self._parse_log_line(path, line_number, line)]
                if self._matches_log_filter(timestamp, entry.level, level, range_, now)
            ]
        except (OSError, UnicodeError) as exc:
            raise self._log_file_error() from exc
        entries.sort(key=lambda item: (item[0], item[1].id), reverse=True)
        return [entry for _, entry in entries]

    def download_logs(
        self,
        *,
        level: LogLevel | Literal["all"] = "all",
        range_: LogRange = "all",
    ) -> bytes:
        """按页面相同口径生成 UTF-8 JSON Lines 下载内容。"""

        entries = self.list_logs(level=level, range_=range_)
        if not entries:
            return b""
        content = "\n".join(
            json.dumps(
                entry.model_dump(by_alias=True),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for entry in entries
        )
        return bytes(f"{content}\n", "utf-8")

    def delete_logs(
        self,
        *,
        level: LogLevel | Literal["all"] = "all",
        range_: LogRange = "all",
    ) -> int:
        """在日志锁内原子重写文件，仅删除命中筛选条件的行。"""

        removed = 0
        now = datetime.now(UTC)
        try:
            with pause_application_file_logging(self.settings):
                for path in self._managed_log_files():
                    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                    retained: list[str] = []
                    changed = False
                    for line_number, line in enumerate(lines, start=1):
                        timestamp, entry = self._parse_log_line(
                            path,
                            line_number,
                            line.rstrip("\r\n"),
                        )
                        if self._matches_log_filter(
                            timestamp,
                            entry.level,
                            level,
                            range_,
                            now,
                        ):
                            removed += 1
                            changed = True
                        else:
                            retained.append(line)
                    if changed:
                        self._replace_log_file(path, retained)
        except (OSError, UnicodeError) as exc:
            raise self._log_file_error() from exc

        logger.info(
            "日志删除完成",
            extra={
                "event": "logs_deleted",
                "business_ids": {
                    "count": removed,
                    "level": level,
                    "range": range_,
                },
            },
        )
        return removed

    async def test_model(
        self,
        group: ModelGroup,
        payload: ModelEndpointPayload,
    ) -> ConnectionTestResult:
        """实际调用当前参数，并持久化不含明文的测试凭据。"""

        config = self._get_config(group)
        base_url = self._validated_base_url(payload.base_url)
        api_key = self._resolve_api_key(config, payload.api_key)
        extra_body = self._validated_extra_body(group, payload.extra_body)

        if group == "main":
            # 额外参数一并发出去：写错的路由或字段必须在这里就被上游打回，
            # 而不是等保存之后让每一轮对话都失败。
            await self.gateway.test_main(
                base_url=base_url,
                model=payload.model,
                api_key=api_key,
                extra_body=parse_extra_body(extra_body),
            )
            tested_dimension = None
            message = "主模型连接测试成功"
        else:
            tested_dimension = await self.gateway.test_embedding(
                base_url=base_url,
                model=payload.model,
                api_key=api_key,
            )
            message = f"Embedding 连接测试成功，向量维度为 {tested_dimension}"

        config.tested_fingerprint = self._fingerprint(
            base_url=base_url,
            model=payload.model,
            api_key=api_key,
            extra_body=extra_body,
        )
        config.tested_vector_dimension = tested_dimension
        config.tested_at = utc_now()
        self.session.commit()
        return ConnectionTestResult(ok=True, message=message)

    def save_model(
        self,
        group: ModelGroup,
        payload: ModelEndpointPayload,
    ) -> ModelSettings:
        """仅保存与最近成功测试完全一致的参数。"""

        config = self._get_config(group)
        base_url = self._validated_base_url(payload.base_url)
        extra_body = self._validated_extra_body(group, payload.extra_body)
        old_api_key = unprotect_api_key(config.secret_ref) if config.secret_ref else None
        api_key = payload.api_key or old_api_key
        if api_key is None:
            raise self._missing_api_key_error()

        fingerprint = self._fingerprint(
            base_url=base_url,
            model=payload.model,
            api_key=api_key,
            extra_body=extra_body,
        )
        if config.tested_fingerprint != fingerprint:
            raise AppError(
                status_code=409,
                code="MODEL_CONFIG_NOT_TESTED",
                message="当前模型参数尚未通过连接测试",
            )

        if group == "embed" and config.tested_vector_dimension is None:
            raise AppError(
                status_code=409,
                code="MODEL_CONFIG_NOT_TESTED",
                message="Embedding 参数尚未通过有效向量测试",
            )

        if group == "main":
            active_changed = (
                config.base_url != base_url
                or config.model_name != payload.model
                or config.extra_body_json != extra_body
                or old_api_key != api_key
            )
            if active_changed:
                config.config_version += 1
        else:
            was_configured = config.config_version > 0
            vector_changed = (
                config.base_url != base_url
                or config.model_name != payload.model
                or config.vector_dimension != config.tested_vector_dimension
            )
            if not was_configured or vector_changed:
                config.config_version += 1
            if was_configured and vector_changed:
                config.rebuild_required = 1
                self._mark_all_indexes_stale()
            config.vector_dimension = config.tested_vector_dimension

        config.base_url = base_url
        config.model_name = payload.model
        config.extra_body_json = extra_body
        if payload.api_key:
            config.secret_ref = protect_api_key(payload.api_key)
            config.key_tail = api_key_tail(payload.api_key)
        config.updated_at = utc_now()
        self.session.commit()
        logger.info(
            "模型配置已保存",
            extra={
                "event": "model_config_saved",
                "business_ids": {
                    "group": group,
                    "configVersion": config.config_version,
                },
            },
        )
        return self.get_model_settings()

    def _managed_log_files(self) -> list[Path]:
        """只返回日志目录内当前文件及数字后缀滚动备份。"""

        logs_dir = self.settings.logs_dir.resolve()
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_name_pattern = re.compile(rf"{re.escape(LOG_FILE_NAME)}(?:\.\d+)?")
        return sorted(
            (
                candidate.resolve()
                for candidate in logs_dir.iterdir()
                if candidate.is_file()
                and file_name_pattern.fullmatch(candidate.name)
                and candidate.resolve().parent == logs_dir
            ),
            key=lambda path: path.name,
        )

    def _parse_log_line(
        self,
        path: Path,
        line_number: int,
        line: str,
    ) -> tuple[datetime, LogEntry]:
        """解析一行结构化日志；损坏行只返回不含原文的安全占位。"""

        entry_id = sha256(bytes(f"{path.name}:{line_number}:{line}", "utf-8")).hexdigest()[:24]
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError("日志记录不是对象")
            timestamp_text = raw["timestamp"]
            level = raw["level"]
            if not isinstance(timestamp_text, str) or level not in {
                "DEBUG",
                "INFO",
                "WARNING",
                "ERROR",
            }:
                raise ValueError("日志时间或级别无效")
            timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("日志时间缺少时区")
            timestamp = timestamp.astimezone(UTC)
            module = raw["module"]
            event = raw["event"]
            request_id = raw["requestId"]
            message = raw["message"]
            business_ids = raw.get("businessIds", {})
            if not all(
                isinstance(value, str) for value in (module, event, request_id, message)
            ) or not isinstance(business_ids, dict):
                raise ValueError("日志结构无效")
            safe_business_ids = {
                sanitize_log_text(key): sanitize_log_text(value)
                for key, value in business_ids.items()
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            timestamp = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            level = "WARNING"
            module = "app.core.logging"
            event = "log_parse_failed"
            request_id = "-"
            message = "日志记录格式损坏"
            safe_business_ids = {"file": path.name, "line": str(line_number)}

        local_time = timestamp.astimezone()
        return timestamp, LogEntry(
            id=entry_id,
            date=local_time.strftime("%Y-%m-%d"),
            time=local_time.strftime("%H:%M:%S"),
            level=level,
            module=sanitize_log_text(module),
            event=sanitize_log_text(event),
            request_id=sanitize_log_text(request_id),
            message=sanitize_log_text(message),
            business_ids=safe_business_ids,
        )

    @staticmethod
    def _matches_log_filter(
        timestamp: datetime,
        entry_level: LogLevel,
        level: LogLevel | Literal["all"],
        range_: LogRange,
        now: datetime,
    ) -> bool:
        """使用统一时间边界判断日志是否命中查询、下载或删除条件。"""

        if level != "all" and entry_level != level:
            return False
        if range_ == "all":
            return True
        days = {"1d": 1, "7d": 7, "30d": 30}[range_]
        return timestamp >= now - timedelta(days=days)

    @staticmethod
    def _replace_log_file(path: Path, retained: list[str]) -> None:
        """在同一目录写入临时文件后原子替换目标日志。"""

        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                handle.writelines(retained)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _log_file_error() -> AppError:
        """把本地文件失败转换为不暴露路径和原始异常的业务错误。"""

        return AppError(
            status_code=500,
            code="LOG_FILE_OPERATION_FAILED",
            message="日志文件操作失败",
        )

    def _build_full_export_files(self, exported_at: str) -> dict[str, object]:
        """按固定文件名构造完整业务导出，排除密钥和可重建运行状态。"""

        identity = self.session.scalar(
            select(UserIdentity).where(UserIdentity.singleton_key == "current")
        )
        if identity is None:
            raise RuntimeError("缺少当前用户身份")

        identity_data = self._serialize_identity(identity)
        characters_data = [
            self._serialize_character(character)
            for character in self.session.scalars(
                select(Character).order_by(Character.created_at, Character.id)
            ).all()
        ]
        worldbooks_data = self._serialize_world_books()
        sessions_data = self._serialize_sessions()
        messages_data = self._serialize_messages()
        memories_data = self._serialize_memories()
        revision = self.session.scalar(text("SELECT version_num FROM alembic_version"))
        if revision is None:
            raise RuntimeError("缺少 Alembic revision")

        counts = {
            "identities": 1,
            "characters": len(characters_data),
            "worldBooks": len(worldbooks_data),
            "worldBookEntries": sum(len(book["entries"]) for book in worldbooks_data),
            "sessions": len(sessions_data),
            "messages": len(messages_data),
            "messageBlocks": sum(len(message["blocks"]) for message in messages_data),
            "memories": len(memories_data),
            "memorySources": sum(len(memory["sources"]) for memory in memories_data),
        }
        return {
            "manifest.json": {
                "schemaVersion": EXPORT_SCHEMA_VERSION,
                "exportedAt": exported_at,
                "appVersion": self.settings.app_version,
                "alembicRevision": str(revision),
                "counts": counts,
            },
            "identity.json": identity_data,
            "characters.json": characters_data,
            "worldbooks.json": worldbooks_data,
            "sessions.json": sessions_data,
            "messages.json": messages_data,
            "memories.json": memories_data,
        }

    def _serialize_world_books(self) -> list[dict[str, object]]:
        """导出世界书原文与正式条目，不携带派生索引状态。"""

        books = list(
            self.session.scalars(
                select(WorldBook).order_by(WorldBook.created_at, WorldBook.id)
            ).all()
        )
        entries = self.session.scalars(
            select(WorldBookEntry).order_by(
                WorldBookEntry.world_book_id,
                WorldBookEntry.position,
                WorldBookEntry.id,
            )
        ).all()
        entries_by_book: dict[str, list[WorldBookEntry]] = defaultdict(list)
        for entry in entries:
            entries_by_book[entry.world_book_id].append(entry)
        return [
            {
                "id": book.id,
                "name": book.name,
                "rawContent": book.raw_content,
                "createdAt": book.created_at,
                "updatedAt": book.updated_at,
                "entries": [
                    self._serialize_world_book_entry(entry) for entry in entries_by_book[book.id]
                ],
            }
            for book in books
        ]

    def _serialize_sessions(self) -> list[dict[str, object]]:
        """导出全部会话及其当前身份、角色卡与世界书绑定。"""

        chat_sessions = list(
            self.session.scalars(
                select(ChatSession).order_by(ChatSession.created_at, ChatSession.id)
            ).all()
        )
        identity = self.session.scalar(
            select(UserIdentity).where(UserIdentity.singleton_key == "current")
        )
        if identity is None:
            raise RuntimeError("缺少当前用户身份记录")
        return [
            session_to_dto(self.repository, chat_session, identity).model_dump(by_alias=True)
            for chat_session in chat_sessions
        ]

    def _serialize_messages(self, *, session_id: str | None = None) -> list[dict[str, object]]:
        """按会话与位置稳定导出消息，并保持块序号和类型。"""

        statement = select(MessageModel)
        if session_id is not None:
            statement = statement.where(MessageModel.session_id == session_id)
        messages = list(
            self.session.scalars(
                statement.order_by(
                    MessageModel.session_id,
                    MessageModel.position,
                    MessageModel.id,
                )
            ).all()
        )
        if not messages:
            return []

        message_ids = [message.id for message in messages]
        blocks = self.session.scalars(
            select(MessageBlockModel)
            .where(MessageBlockModel.message_id.in_(message_ids))
            .order_by(
                MessageBlockModel.message_id,
                MessageBlockModel.sequence,
                MessageBlockModel.id,
            )
        ).all()
        blocks_by_message: dict[str, list[MessageBlockModel]] = defaultdict(list)
        for block in blocks:
            blocks_by_message[block.message_id].append(block)

        return [
            {
                "id": message.id,
                "sessionId": message.session_id,
                "position": message.position,
                "turnNumber": message.turn_number,
                "role": message.role,
                "retrieved": (
                    json.loads(message.retrieved_entries_json)
                    if message.retrieved_entries_json is not None
                    else []
                ),
                "createdAt": message.created_at,
                "blocks": [
                    {
                        "id": block.id,
                        "sequence": block.sequence,
                        "type": block.type,
                        "content": block.content,
                    }
                    for block in blocks_by_message[message.id]
                ],
            }
            for message in messages
        ]

    def _serialize_memories(self) -> list[dict[str, object]]:
        """导出长期记忆业务状态与有序来源关系，不携带向量状态。"""

        memories = list(
            self.session.scalars(
                select(LongTermMemory).order_by(
                    LongTermMemory.created_at,
                    LongTermMemory.id,
                )
            ).all()
        )
        sources = self.session.scalars(
            select(MemorySource).order_by(
                MemorySource.memory_id,
                MemorySource.source_order,
                MemorySource.message_id,
            )
        ).all()
        sources_by_memory: dict[str, list[MemorySource]] = defaultdict(list)
        for source in sources:
            sources_by_memory[source.memory_id].append(source)
        return [
            {
                "id": memory.id,
                "characterId": memory.character_id,
                "type": memory.type,
                "content": memory.content,
                "importance": memory.importance,
                "status": memory.status,
                "sourceLabel": memory.source_label,
                "memoryVersion": memory.memory_version,
                "createdAt": memory.created_at,
                "updatedAt": memory.updated_at,
                "sources": [
                    {
                        "messageId": source.message_id,
                        "sourceOrder": source.source_order,
                    }
                    for source in sources_by_memory[memory.id]
                ],
            }
            for memory in memories
        ]

    @staticmethod
    def _serialize_identity(identity: UserIdentity) -> dict[str, object]:
        """序列化当前身份的全部业务字段。"""

        return {
            "id": identity.id,
            "name": identity.name,
            "personaName": identity.persona_name,
            "bio": identity.bio,
            "createdAt": identity.created_at,
            "updatedAt": identity.updated_at,
        }

    @staticmethod
    def _serialize_character(character: Character) -> dict[str, object]:
        """序列化角色卡并把对话示例还原为 JSON 数组。"""

        return {
            "id": character.id,
            "name": character.name,
            "introduction": character.introduction,
            "systemPrompt": character.system_prompt,
            "dialogueExamples": json.loads(character.dialogue_examples_json),
            "createdAt": character.created_at,
            "updatedAt": character.updated_at,
        }

    @staticmethod
    def _serialize_world_book_entry(entry: WorldBookEntry) -> dict[str, object]:
        """序列化世界书正式条目的可恢复业务字段。"""

        return {
            "id": entry.id,
            "worldBookId": entry.world_book_id,
            "position": entry.position,
            "name": entry.name,
            "content": entry.content,
            "keywords": json.loads(entry.keywords_json),
            "category": entry.category,
            "resident": bool(entry.resident),
            "enabled": bool(entry.enabled),
            "createdAt": entry.created_at,
            "updatedAt": entry.updated_at,
        }

    def _require_identity(self) -> UserIdentity:
        """读取单用户当前身份，缺失表示数据库初始化状态损坏。"""

        identity = self.session.scalar(
            select(UserIdentity).where(UserIdentity.singleton_key == "current")
        )
        if identity is None:
            raise RuntimeError("缺少当前用户身份记录")
        return identity

    @staticmethod
    def _json_bytes(payload: object) -> bytes:
        """按 UTF-8 和固定缩进生成可读 JSON 文件内容。"""

        return (
            json.dumps(payload, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n"
        ).encode("utf-8")

    @staticmethod
    def _is_managed_export(path: Path, exports_dir: Path) -> bool:
        """校验目标是导出目录内由系统创建的随机 ZIP。"""

        resolved = path.resolve()
        return (
            resolved.parent == exports_dir
            and resolved.name.startswith("export-")
            and resolved.suffix == ".zip"
        )

    def _mark_all_indexes_stale(self) -> None:
        """Embedding 向量空间变化时，使全部可索引事实记录停止就绪。"""

        stale_values = {"index_status": "stale", "last_index_error": None}
        self.session.execute(update(WorldBookEntry).values(**stale_values))
        self.session.execute(
            update(LongTermMemory).where(LongTermMemory.status == "active").values(**stale_values)
        )

    def _get_config(self, group: ModelGroup) -> ModelConfig:
        """读取迁移保证存在的固定配置行。"""

        config = self.repository.get(ModelConfig, group)
        if config is None:
            raise RuntimeError(f"缺少模型配置记录：{group}")
        return config

    @staticmethod
    def _public_config(config: ModelConfig) -> ModelEndpointConfig:
        """将内部密钥引用转换为前端安全状态。"""

        return ModelEndpointConfig(
            base_url=config.base_url,
            model=config.model_name,
            key_set=config.secret_ref is not None,
            key_tail=config.key_tail if config.secret_ref is not None else "",
            extra_body=config.extra_body_json,
        )

    @staticmethod
    def _validated_extra_body(group: ModelGroup, extra_body: str) -> str:
        """额外请求参数只对主模型开放，Embedding 端点提交非空值一律拒绝。"""

        cleaned = extra_body.strip()
        if cleaned and group != "main":
            raise AppError(
                status_code=422,
                code="VALIDATION_FAILED",
                message="额外请求参数仅对主模型有效",
            )
        return cleaned

    @staticmethod
    def _validated_base_url(base_url: str) -> str:
        """把地址语义错误转换为稳定的 422 响应。"""

        try:
            return normalize_base_url(base_url)
        except ValueError as exc:
            raise AppError(
                status_code=422,
                code="VALIDATION_ERROR",
                message=str(exc),
            ) from exc

    @staticmethod
    def _fingerprint(
        *,
        base_url: str,
        model: str,
        api_key: str,
        extra_body: str = "",
    ) -> str:
        """只保存参数摘要，不在测试状态中保存 API Key。"""

        fields: dict[str, str] = {
            "baseUrl": base_url,
            "model": model,
            "apiKeyHash": sha256(api_key.encode("utf-8")).hexdigest(),
        }
        # 没填额外参数时不写进摘要，老配置的指纹保持不变，不会平白要求重测。
        if extra_body:
            fields["extraBody"] = extra_body
        payload = json.dumps(
            fields,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _resolve_api_key(config: ModelConfig, submitted_api_key: str) -> str:
        """空字符串表示复用已保存密钥，而不是清空。"""

        if submitted_api_key:
            return submitted_api_key
        if config.secret_ref:
            return unprotect_api_key(config.secret_ref)
        raise SystemService._missing_api_key_error()

    @staticmethod
    def _missing_api_key_error() -> AppError:
        """构造测试和保存共用的缺失密钥错误。"""

        return AppError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="尚未配置 API Key",
        )
