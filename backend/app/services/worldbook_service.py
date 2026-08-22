"""世界书原文、拆分草稿与正式条目的持久化规则。"""

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.chains.worldbook_split_chain import build_worldbook_split_chain
from app.core.exceptions import AppError
from app.core.security import unprotect_api_key
from app.db.models import BackgroundTask, ChatSession, ModelConfig, utc_now
from app.db.models import WorldBook as WorldBookModel
from app.db.models import WorldBookEntry as WorldBookEntryModel
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import (
    ChromaRepository,
    build_worldbook_index_text,
    calculate_content_hash,
    worldbook_entry_document_id,
)
from app.repositories.sqlite_repository import SQLiteRepository
from app.schemas.dto import (
    ConfirmWorldBookDraftsPayload,
    ReembedResult,
    UpdateWorldBookEntryPayload,
    UpdateWorldBookPayload,
    WorldBook,
    WorldBookDraftEntry,
    WorldBookEntry,
)

logger = logging.getLogger(__name__)

IndexSourceState = tuple[str, bool, bool]

KEYWORD_LINE_PREFIX = "触发关键词："


def strip_duplicate_keyword_line(content: str, keywords: list[str]) -> str:
    """剥离正文开头与 keywords 字段逐字重复的「触发关键词：」行。

    该行来自世界书原文，信息已完整提取进 keywords 字段（PRD §3.3 要求提取为
    结构化字段），继续留在正文里只会和界面下方的关键词框重复展示。

    只在整行与 `'、'.join(keywords)` 逐字相符、且其后紧跟换行时才剥离；
    关键词是正文首词的前缀这类情况（关键词「世界」撞上正文「世界观…」）保持原样，
    正文被剥空时也保持原样，避免误删用户设定。
    """

    prefix = f"{KEYWORD_LINE_PREFIX}{'、'.join(keywords)}"
    if not content.startswith(prefix):
        return content
    rest = content[len(prefix) :]
    if rest and not rest.startswith("\n"):
        return content
    body = rest.lstrip("\n")
    return body or content


class WorldBookService:
    """管理世界书业务事实，并为后续索引同步维护准确状态。"""

    def __init__(
        self,
        session: Session,
        gateway: ModelGateway,
        chroma_repository: ChromaRepository,
    ) -> None:
        self.session = session
        self.repository = SQLiteRepository(session)
        self.gateway = gateway
        self.chroma = chroma_repository

    def list_world_books(self) -> list[WorldBook]:
        """按最近更新时间倒序返回全部世界书及其有序条目。"""

        books = self.repository.list_all(
            WorldBookModel,
            order_by=(WorldBookModel.updated_at.desc(), WorldBookModel.id),
        )
        embedding_version = self._get_embedding_config().config_version
        return [self._world_book_to_dto(book, embedding_version) for book in books]

    def get_world_book(self, world_book_id: str) -> WorldBook:
        """返回指定世界书及其按位置排列的正式条目。"""

        return self._world_book_to_dto(
            self._get_world_book_model(world_book_id),
            self._get_embedding_config().config_version,
        )

    def create_world_book(self) -> WorldBook:
        """按 API 契约创建空世界书。"""

        book = WorldBookModel(name="新世界书", raw_content="")
        self.repository.add(book)
        self.session.commit()
        return self._world_book_to_dto(book, self._get_embedding_config().config_version)

    def update_world_book(
        self,
        world_book_id: str,
        payload: UpdateWorldBookPayload,
    ) -> WorldBook:
        """仅更新请求中实际提交的名称或完整原文。"""

        book = self._get_world_book_model(world_book_id)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(book, field, value)
        book.updated_at = utc_now()
        self.session.commit()
        return self._world_book_to_dto(book, self._get_embedding_config().config_version)

    def delete_world_book(self, world_book_id: str) -> None:
        """解绑引用会话后删除世界书，并在提交后清理其全部派生向量。"""

        book = self._get_world_book_model(world_book_id)
        # 解绑由 sessions.world_book_id 的 ON DELETE SET NULL 完成，这里只做记录。
        unbound = self.session.scalar(
            select(func.count(ChatSession.id)).where(
                ChatSession.world_book_id == world_book_id
            )
        )
        if unbound:
            logger.info(
                "世界书删除前已解绑引用会话",
                extra={
                    "event": "world_book_sessions_unbound",
                    "business_ids": {"worldBookId": world_book_id, "sessionCount": unbound},
                },
            )
        entry_ids = list(
            self.session.scalars(
                select(WorldBookEntryModel.id).where(
                    WorldBookEntryModel.world_book_id == world_book_id
                )
            ).all()
        )
        self.repository.delete(book)
        self.session.commit()
        self._delete_vectors_or_schedule(entry_ids)

    async def split_world_book(self, world_book_id: str) -> list[WorldBookDraftEntry]:
        """使用当前主模型拆分原文，草稿只随本次调用返回。"""

        book = self._get_world_book_model(world_book_id)
        if not book.raw_content.strip():
            raise AppError(
                status_code=400,
                code="EMPTY_SOURCE_CONTENT",
                message="世界书原文为空，不能拆分",
            )
        config = self._require_main_model_config()
        chain = build_worldbook_split_chain(
            gateway=self.gateway,
            base_url=config.base_url,
            model=config.model_name,
            api_key=unprotect_api_key(config.secret_ref or ""),
        )
        return await chain.ainvoke(book.raw_content)

    async def confirm_drafts(
        self,
        world_book_id: str,
        payload: ConfirmWorldBookDraftsPayload,
    ) -> list[WorldBookEntry]:
        """先提交正式条目，再尝试生成并写入对应向量。"""

        self._get_world_book_model(world_book_id)
        last_position = self.session.scalar(
            select(func.max(WorldBookEntryModel.position)).where(
                WorldBookEntryModel.world_book_id == world_book_id
            )
        )
        next_position = (last_position if last_position is not None else -1) + 1
        entries: list[WorldBookEntryModel] = []

        for offset, draft in enumerate(payload.drafts):
            entry = WorldBookEntryModel(
                world_book_id=world_book_id,
                position=next_position + offset,
                name=draft.name,
                content=strip_duplicate_keyword_line(draft.content, draft.keywords),
                keywords_json=json.dumps(
                    draft.keywords,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                category=draft.category,
                resident=int(draft.resident),
                enabled=1,
                index_status="pending",
                content_hash="",
            )
            entry.content_hash = self._calculate_entry_hash(entry)
            self.repository.add(entry)
            entries.append(entry)

        self.session.commit()
        source_states = {
            entry.id: (
                entry.content_hash,
                bool(entry.enabled),
                bool(entry.resident),
            )
            for entry in entries
        }

        try:
            await self._index_entries(world_book_id, entries, source_states)
        except AppError as exc:
            self._mark_index_failure(source_states, exc.message)
            logger.warning(
                "世界书条目索引失败 world_book_id=%s code=%s",
                world_book_id,
                exc.code,
            )

        return self._entries_to_dto(list(source_states))

    def create_entry(self, world_book_id: str) -> WorldBookEntry:
        """在世界书末尾创建一个待索引的默认正式条目。"""

        self._get_world_book_model(world_book_id)
        position = self.session.scalar(
            select(func.max(WorldBookEntryModel.position)).where(
                WorldBookEntryModel.world_book_id == world_book_id
            )
        )
        entry = WorldBookEntryModel(
            world_book_id=world_book_id,
            position=(position if position is not None else -1) + 1,
            name="新条目",
            content="在这里填写条目内容，保存后将自动生成 Embedding。",
            keywords_json='["关键词"]',
            category="未分类",
            resident=0,
            enabled=1,
            index_status="pending",
            content_hash="",
        )
        entry.content_hash = self._calculate_entry_hash(entry)
        self.repository.add(entry)
        self.session.commit()
        return self._entry_to_dto(entry, self._get_embedding_config().config_version)

    def update_entry(
        self,
        world_book_id: str,
        entry_id: str,
        payload: UpdateWorldBookEntryPayload,
    ) -> WorldBookEntry:
        """更新条目字段，任何正文或 metadata 变化都标记向量过期。"""

        self._get_world_book_model(world_book_id)
        entry = self._get_entry_model(world_book_id, entry_id)
        changes = payload.model_dump(exclude_unset=True)
        index_text_changed = False
        index_source_changed = False

        if "keywords" in changes:
            keywords_json = json.dumps(
                changes.pop("keywords"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if entry.keywords_json != keywords_json:
                entry.keywords_json = keywords_json
                index_text_changed = True
                index_source_changed = True

        for field, value in changes.items():
            stored_value = int(value) if field in {"resident", "enabled"} else value
            if getattr(entry, field) == stored_value:
                continue
            setattr(entry, field, stored_value)
            index_source_changed = True
            if field in {"name", "content", "category"}:
                index_text_changed = True

        if index_text_changed:
            entry.content_hash = self._calculate_entry_hash(entry)
        if index_source_changed:
            entry.index_status = "stale"
            entry.last_index_error = None
        entry.updated_at = utc_now()
        self.session.commit()
        return self._entry_to_dto(entry, self._get_embedding_config().config_version)

    def delete_entry(self, world_book_id: str, entry_id: str) -> None:
        """先删除 SQLite 正式条目，再清理其确定 ID 向量。"""

        self._get_world_book_model(world_book_id)
        entry = self._get_entry_model(world_book_id, entry_id)
        self.repository.delete(entry)
        self.session.commit()
        self._delete_vectors_or_schedule([entry_id])

    async def reembed(self, world_book_id: str) -> ReembedResult:
        """仅重建当前世界书中待处理或版本过期的正式条目。"""

        self._get_world_book_model(world_book_id)
        current_version = self._get_embedding_config().config_version
        entries = list(
            self.session.scalars(
                select(WorldBookEntryModel)
                .where(
                    WorldBookEntryModel.world_book_id == world_book_id,
                    (
                        (WorldBookEntryModel.index_status != "ready")
                        | (WorldBookEntryModel.embedding_version != current_version)
                        | (WorldBookEntryModel.embedding_version.is_(None))
                    ),
                )
                .order_by(WorldBookEntryModel.position, WorldBookEntryModel.id)
            ).all()
        )
        if not entries:
            return ReembedResult(count=0)

        source_states = {
            entry.id: (entry.content_hash, bool(entry.enabled), bool(entry.resident))
            for entry in entries
        }
        try:
            await self._index_entries(world_book_id, entries, source_states)
        except AppError as exc:
            self._mark_index_failure(source_states, exc.message)
            raise

        ready_count = sum(
            entry.index_status == "ready" and entry.embedding_version == current_version
            for entry in self._load_entries(list(source_states))
        )
        return ReembedResult(count=ready_count)

    def _get_world_book_model(self, world_book_id: str) -> WorldBookModel:
        """读取世界书，并将不存在转换为稳定业务错误。"""

        book = self.repository.get(WorldBookModel, world_book_id)
        if book is None:
            raise AppError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="世界书不存在",
            )
        return book

    def _get_entry_model(self, world_book_id: str, entry_id: str) -> WorldBookEntryModel:
        """读取属于指定世界书的条目，阻止跨世界书修改。"""

        entry = self.session.scalar(
            select(WorldBookEntryModel).where(
                WorldBookEntryModel.id == entry_id,
                WorldBookEntryModel.world_book_id == world_book_id,
            )
        )
        if entry is None:
            raise AppError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="世界书条目不存在",
            )
        return entry

    def _get_embedding_config(self) -> ModelConfig:
        """读取迁移保证存在的 Embedding 固定配置行。"""

        config = self.repository.get(ModelConfig, "embed")
        if config is None:
            raise RuntimeError("缺少 Embedding 模型配置记录")
        return config

    def _require_main_model_config(self) -> ModelConfig:
        """拆分前确认主模型配置完整。"""

        config = self.repository.get(ModelConfig, "main")
        if config is None or not config.base_url or not config.model_name or not config.secret_ref:
            raise AppError(
                status_code=503,
                code="MODEL_NOT_CONFIGURED",
                message="主模型尚未配置",
            )
        return config

    def _require_embedding_config(self) -> ModelConfig:
        """确认 Embedding 配置可以生成当前版本向量。"""

        config = self._get_embedding_config()
        if (
            not config.base_url
            or not config.model_name
            or not config.secret_ref
            or config.vector_dimension is None
        ):
            raise AppError(
                status_code=503,
                code="MODEL_NOT_CONFIGURED",
                message="Embedding 模型尚未配置",
            )
        return config

    async def _index_entries(
        self,
        world_book_id: str,
        entries: list[WorldBookEntryModel],
        source_states: dict[str, IndexSourceState],
    ) -> None:
        """生成向量、写入 Chroma，并在复核事实未变化后标记就绪。"""

        config = self._require_embedding_config()
        target_version = config.config_version
        documents = [
            build_worldbook_index_text(
                name=entry.name,
                category=entry.category,
                keywords=json.loads(entry.keywords_json),
                content=entry.content,
            )
            for entry in entries
        ]
        try:
            api_key = unprotect_api_key(config.secret_ref or "")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise AppError(
                status_code=503,
                code="MODEL_NOT_CONFIGURED",
                message="Embedding 模型密钥不可用",
            ) from exc

        embeddings = await self.gateway.create_embeddings(
            base_url=config.base_url,
            model=config.model_name,
            api_key=api_key,
            texts=documents,
            expected_dimension=config.vector_dimension,
        )

        self.session.expire_all()
        if not self._index_sources_match(source_states, target_version):
            self._mark_sources_stale(source_states, "索引源在向量生成期间发生变化")
            return

        ids = list(source_states)
        metadatas = [
            {
                "sourceKind": "liveEntry",
                "entryId": entry_id,
                "worldBookId": world_book_id,
                "enabled": source_states[entry_id][1],
                "resident": source_states[entry_id][2],
                "embeddingVersion": target_version,
                "contentHash": source_states[entry_id][0],
            }
            for entry_id in ids
        ]
        try:
            self.chroma.upsert_worldbook(
                ids=[worldbook_entry_document_id(entry_id) for entry_id in ids],
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )
        except Exception as exc:
            raise AppError(
                status_code=500,
                code="INDEX_OPERATION_FAILED",
                message="世界书条目向量写入失败",
            ) from exc

        self.session.expire_all()
        current_version = self._get_embedding_config().config_version
        for entry in self._load_entries(ids):
            expected_state = source_states.get(entry.id)
            current_state = (entry.content_hash, bool(entry.enabled), bool(entry.resident))
            if current_version == target_version and current_state == expected_state:
                entry.index_status = "ready"
                entry.embedding_version = target_version
                entry.last_index_error = None
            else:
                entry.index_status = "stale"
                entry.last_index_error = "索引源在向量写入期间发生变化"
        self.session.commit()

    def _index_sources_match(
        self,
        source_states: dict[str, IndexSourceState],
        target_version: int,
    ) -> bool:
        """复核 Embedding 版本、正文哈希与向量 metadata 来源未变化。"""

        if self._get_embedding_config().config_version != target_version:
            return False
        entries = self._load_entries(list(source_states))
        if len(entries) != len(source_states):
            return False
        return all(
            (entry.content_hash, bool(entry.enabled), bool(entry.resident))
            == source_states[entry.id]
            for entry in entries
        )

    def _mark_index_failure(
        self,
        source_states: dict[str, IndexSourceState],
        message: str,
    ) -> None:
        """外部索引失败时保留正式条目，并写入脱敏失败状态。"""

        self.session.expire_all()
        for entry in self._load_entries(list(source_states)):
            current_state = (entry.content_hash, bool(entry.enabled), bool(entry.resident))
            if current_state == source_states[entry.id]:
                entry.index_status = "failed"
                entry.embedding_version = None
                entry.last_index_error = message
            else:
                entry.index_status = "stale"
        self.session.commit()

    def _mark_sources_stale(
        self,
        source_states: dict[str, IndexSourceState],
        message: str,
    ) -> None:
        """索引输入在外部调用期间变化时阻止旧结果进入就绪状态。"""

        for entry in self._load_entries(list(source_states)):
            entry.index_status = "stale"
            entry.last_index_error = message
        self.session.commit()

    def _delete_vectors_or_schedule(self, entry_ids: list[str]) -> None:
        """删除已失去事实来源的向量；失败时按条目记录可恢复补偿任务。"""

        if not entry_ids:
            return
        try:
            self.chroma.delete_worldbook(
                ids=[worldbook_entry_document_id(entry_id) for entry_id in entry_ids]
            )
        except Exception:
            logger.warning("世界书向量清理失败，已记录补偿任务", exc_info=True)
            for entry_id in entry_ids:
                self.repository.add(
                    BackgroundTask(
                        task_type="vector_cleanup",
                        status="pending",
                        scope_id=worldbook_entry_document_id(entry_id),
                        progress_current=0,
                        progress_total=1,
                        error_message="世界书向量待清理",
                    )
                )
            self.session.commit()

    def _load_entries(self, entry_ids: list[str]) -> list[WorldBookEntryModel]:
        """按世界书内稳定位置读取一组正式条目。"""

        return list(
            self.session.scalars(
                select(WorldBookEntryModel)
                .where(WorldBookEntryModel.id.in_(entry_ids))
                .order_by(WorldBookEntryModel.position, WorldBookEntryModel.id)
            ).all()
        )

    def _entries_to_dto(self, entry_ids: list[str]) -> list[WorldBookEntry]:
        """使用当前 Embedding 版本返回一组正式条目。"""

        embedding_version = self._get_embedding_config().config_version
        return [
            self._entry_to_dto(entry, embedding_version) for entry in self._load_entries(entry_ids)
        ]

    def _world_book_to_dto(self, book: WorldBookModel, embedding_version: int) -> WorldBook:
        """将世界书和按稳定位置排序的条目转换为公开 DTO。"""

        entries = self.session.scalars(
            select(WorldBookEntryModel)
            .where(WorldBookEntryModel.world_book_id == book.id)
            .order_by(WorldBookEntryModel.position, WorldBookEntryModel.id)
        ).all()
        return WorldBook(
            id=book.id,
            name=book.name,
            raw_content=book.raw_content,
            entries=[self._entry_to_dto(entry, embedding_version) for entry in entries],
        )

    @staticmethod
    def _entry_to_dto(entry: WorldBookEntryModel, embedding_version: int) -> WorldBookEntry:
        """转换正式条目，并根据状态与版本计算索引是否过期。"""

        return WorldBookEntry(
            id=entry.id,
            world_book_id=entry.world_book_id,
            name=entry.name,
            content=entry.content,
            keywords=json.loads(entry.keywords_json),
            category=entry.category,
            resident=bool(entry.resident),
            enabled=bool(entry.enabled),
            index_stale=(
                entry.index_status != "ready" or entry.embedding_version != embedding_version
            ),
        )

    @staticmethod
    def _calculate_entry_hash(entry: WorldBookEntryModel) -> str:
        """按统一拼接规则计算世界书条目的 UTF-8 内容哈希。"""

        index_text = build_worldbook_index_text(
            name=entry.name,
            category=entry.category,
            keywords=json.loads(entry.keywords_json),
            content=entry.content,
        )
        return calculate_content_hash(index_text)
