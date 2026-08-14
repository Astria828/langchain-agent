"""按当前 Embedding 配置全量重建两个 Chroma Collection。"""

import json
import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.security import unprotect_api_key
from app.db.models import (
    BackgroundTask,
    LongTermMemory,
    ModelConfig,
    SessionContextSnapshot,
    SessionWorldBookEntrySnapshot,
    WorldBookEntry,
    utc_now,
)
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import (
    ChromaRepository,
    Embedding,
    Metadata,
    build_worldbook_index_text,
    calculate_content_hash,
    memory_document_id,
    session_entry_document_id,
    worldbook_entry_document_id,
)

logger = logging.getLogger(__name__)

EMBEDDING_BATCH_SIZE = 32
INDEX_FAILURE_MESSAGE = "索引重建失败"
INDEX_INTERRUPTED_MESSAGE = "应用中断，索引重建任务未完成"

IndexBundle = tuple[list[str], list[str], list[Metadata]]


def recover_interrupted_index_rebuilds(session: Session) -> int:
    """启动时关闭遗留活动任务，并保持全部派生索引不可用、可重试。"""

    task_ids = session.scalars(
        select(BackgroundTask.id).where(
            BackgroundTask.task_type == "index_rebuild",
            BackgroundTask.status.in_(("pending", "running")),
        )
    ).all()
    if not task_ids:
        return 0

    finished_at = utc_now()
    session.execute(
        update(BackgroundTask)
        .where(BackgroundTask.id.in_(task_ids))
        .values(
            status="failed",
            error_message=INDEX_INTERRUPTED_MESSAGE,
            finished_at=finished_at,
        )
    )
    config = session.get(ModelConfig, "embed")
    if config is None:
        raise RuntimeError("缺少 Embedding 模型配置记录")
    config.rebuild_required = 1

    failed_values = {
        "index_status": "failed",
        "embedding_version": None,
        "last_index_error": INDEX_INTERRUPTED_MESSAGE,
    }
    session.execute(update(WorldBookEntry).values(**failed_values))
    session.execute(update(SessionWorldBookEntrySnapshot).values(**failed_values))
    session.execute(
        update(LongTermMemory).where(LongTermMemory.status == "active").values(**failed_values)
    )
    session.commit()
    logger.warning(
        "已恢复中断的索引重建任务",
        extra={
            "event": "index_rebuilds_recovered",
            "business_ids": {"count": len(task_ids)},
        },
    )
    return len(task_ids)


class RebuildIndexTask:
    """同步接口内部执行的可追踪全量索引重建任务。"""

    def __init__(
        self,
        session: Session,
        gateway: ModelGateway,
        chroma_repository: ChromaRepository,
    ) -> None:
        self.session = session
        self.gateway = gateway
        self.chroma = chroma_repository

    async def run(self) -> None:
        """创建唯一任务，完成双 Collection 重建或持久化失败状态。"""

        config = self._require_embedding_config()
        target_version = config.config_version
        task_id = self._create_task(target_version)

        try:
            self._start_task(task_id)
            worldbook_bundle = self._build_worldbook_bundle(target_version)
            memory_bundle = self._build_memory_bundle(target_version)
            self._set_total(
                task_id,
                len(worldbook_bundle[0]) + len(memory_bundle[0]),
            )

            api_key = unprotect_api_key(config.secret_ref or "")
            worldbook_embeddings = await self._embed_documents(
                config,
                api_key,
                worldbook_bundle[1],
            )
            memory_embeddings = await self._embed_documents(
                config,
                api_key,
                memory_bundle[1],
            )

            self._assert_target_unchanged(target_version)
            self._assert_sources_unchanged(
                target_version,
                worldbook_bundle,
                memory_bundle,
            )

            self.chroma.reset_collections()
            self._write_worldbook_batches(task_id, worldbook_bundle, worldbook_embeddings)
            self._write_memory_batches(task_id, memory_bundle, memory_embeddings)

            self._assert_target_unchanged(target_version)
            self._assert_sources_unchanged(
                target_version,
                worldbook_bundle,
                memory_bundle,
            )
            self._finish_success(task_id, target_version)
            logger.info(
                "索引重建任务完成",
                extra={
                    "event": "index_rebuild_completed",
                    "business_ids": {
                        "taskId": task_id,
                        "targetVersion": target_version,
                    },
                },
            )
        except Exception as exc:
            self._finish_failure(task_id)
            logger.exception(
                "索引重建任务失败",
                exc_info=exc,
                extra={
                    "event": "index_rebuild_failed",
                    "business_ids": {"taskId": task_id},
                },
            )
            raise AppError(
                status_code=500,
                code="INDEX_OPERATION_FAILED",
                message=INDEX_FAILURE_MESSAGE,
            ) from exc

    def _require_embedding_config(self) -> ModelConfig:
        """拒绝在 Embedding 配置不完整时创建无效任务。"""

        config = self.session.get(ModelConfig, "embed")
        if (
            config is None
            or not config.base_url
            or not config.model_name
            or not config.secret_ref
            or config.vector_dimension is None
            or config.config_version < 1
        ):
            raise AppError(
                status_code=503,
                code="MODEL_NOT_CONFIGURED",
                message="Embedding 模型尚未配置",
            )
        return config

    def _create_task(self, target_version: int) -> str:
        """利用查询与数据库唯一索引共同阻止并发重建。"""

        active_task_id = self.session.scalar(
            select(BackgroundTask.id).where(
                BackgroundTask.task_type == "index_rebuild",
                BackgroundTask.status.in_(("pending", "running")),
            )
        )
        if active_task_id is not None:
            raise self._already_running_error()

        task = BackgroundTask(
            task_type="index_rebuild",
            status="pending",
            target_version=target_version,
        )
        self.session.add(task)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise self._already_running_error() from exc
        return task.id

    def _start_task(self, task_id: str) -> None:
        """进入运行态，同时停用全部旧版本索引记录。"""

        task = self._get_task(task_id)
        task.status = "running"
        task.started_at = utc_now()
        config = self._get_embedding_config()
        config.rebuild_required = 1
        self._mark_indexable_records("stale", error_message=None)
        self.session.commit()

    def _set_total(self, task_id: str, total: int) -> None:
        """在外部调用前记录本次任务的稳定总量。"""

        task = self._get_task(task_id)
        task.progress_total = total
        self.session.commit()

    async def _embed_documents(
        self,
        config: ModelConfig,
        api_key: str,
        documents: list[str],
    ) -> list[Embedding]:
        """按固定小批次生成向量，避免一次请求承载全部正文。"""

        embeddings: list[Embedding] = []
        for start in range(0, len(documents), EMBEDDING_BATCH_SIZE):
            batch = documents[start : start + EMBEDDING_BATCH_SIZE]
            embeddings.extend(
                await self.gateway.create_embeddings(
                    base_url=config.base_url,
                    model=config.model_name,
                    api_key=api_key,
                    texts=batch,
                    expected_dimension=config.vector_dimension,
                )
            )
        return embeddings

    def _build_worldbook_bundle(self, target_version: int) -> IndexBundle:
        """从正式条目和会话快照生成稳定文档与隔离 metadata。"""

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[Metadata] = []

        entries = self.session.scalars(select(WorldBookEntry).order_by(WorldBookEntry.id)).all()
        for entry in entries:
            index_text = build_worldbook_index_text(
                name=entry.name,
                category=entry.category,
                keywords=self._decode_keywords(entry.keywords_json),
                content=entry.content,
            )
            content_hash = calculate_content_hash(index_text)
            ids.append(worldbook_entry_document_id(entry.id))
            documents.append(index_text)
            metadatas.append(
                {
                    "sourceKind": "liveEntry",
                    "entryId": entry.id,
                    "worldBookId": entry.world_book_id,
                    "enabled": bool(entry.enabled),
                    "resident": bool(entry.resident),
                    "embeddingVersion": target_version,
                    "contentHash": content_hash,
                }
            )

        snapshot_rows = self.session.execute(
            select(SessionWorldBookEntrySnapshot, SessionContextSnapshot)
            .join(
                SessionContextSnapshot,
                SessionContextSnapshot.id == SessionWorldBookEntrySnapshot.context_snapshot_id,
            )
            .order_by(SessionWorldBookEntrySnapshot.id)
        ).all()
        for snapshot, context in snapshot_rows:
            if context.world_book_source_id is None:
                raise ValueError("世界书条目快照缺少来源世界书")
            index_text = build_worldbook_index_text(
                name=snapshot.name,
                category=snapshot.category,
                keywords=self._decode_keywords(snapshot.keywords_json),
                content=snapshot.content,
            )
            content_hash = calculate_content_hash(index_text)
            ids.append(session_entry_document_id(snapshot.id))
            documents.append(index_text)
            metadatas.append(
                {
                    "sourceKind": "sessionSnapshot",
                    "entrySnapshotId": snapshot.id,
                    "sessionId": context.session_id,
                    "worldBookId": context.world_book_source_id,
                    "resident": bool(snapshot.resident),
                    "embeddingVersion": target_version,
                    "contentHash": content_hash,
                }
            )
        return ids, documents, metadatas

    def _build_memory_bundle(self, target_version: int) -> IndexBundle:
        """只为可参与召回的有效长期记忆生成向量。"""

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[Metadata] = []
        memories = self.session.scalars(
            select(LongTermMemory)
            .where(LongTermMemory.status == "active")
            .order_by(LongTermMemory.id)
        ).all()
        for memory in memories:
            content_hash = calculate_content_hash(memory.content)
            ids.append(memory_document_id(memory.id))
            documents.append(memory.content)
            metadatas.append(
                {
                    "memoryId": memory.id,
                    "characterId": memory.character_id,
                    "status": memory.status,
                    "memoryType": memory.type,
                    "embeddingVersion": target_version,
                    "contentHash": content_hash,
                }
            )
        return ids, documents, metadatas

    def _assert_sources_unchanged(
        self,
        target_version: int,
        expected_worldbook: IndexBundle,
        expected_memories: IndexBundle,
    ) -> None:
        """写入前后复核事实数据，防止把并发修改标记为已就绪。"""

        self.session.expire_all()
        if self._build_worldbook_bundle(target_version) != expected_worldbook:
            raise RuntimeError("世界书索引源在重建期间发生变化")
        if self._build_memory_bundle(target_version) != expected_memories:
            raise RuntimeError("长期记忆索引源在重建期间发生变化")

    def _assert_target_unchanged(self, target_version: int) -> None:
        """若配置在任务中途再次变化，拒绝启用本次旧目标结果。"""

        self.session.expire_all()
        if self._get_embedding_config().config_version != target_version:
            raise RuntimeError("Embedding 配置在重建期间发生变化")

    def _write_worldbook_batches(
        self,
        task_id: str,
        bundle: IndexBundle,
        embeddings: list[Embedding],
    ) -> None:
        """分批写入世界书 Collection，并持久化已完成数量。"""

        ids, documents, metadatas = bundle
        for start in range(0, len(ids), EMBEDDING_BATCH_SIZE):
            end = start + EMBEDDING_BATCH_SIZE
            self.chroma.upsert_worldbook(
                ids=ids[start:end],
                embeddings=embeddings[start:end],
                documents=documents[start:end],
                metadatas=metadatas[start:end],
            )
            self._advance_progress(task_id, min(EMBEDDING_BATCH_SIZE, len(ids) - start))

    def _write_memory_batches(
        self,
        task_id: str,
        bundle: IndexBundle,
        embeddings: list[Embedding],
    ) -> None:
        """在世界书完成后分批写入长期记忆 Collection。"""

        ids, documents, metadatas = bundle
        for start in range(0, len(ids), EMBEDDING_BATCH_SIZE):
            end = start + EMBEDDING_BATCH_SIZE
            self.chroma.upsert_memories(
                ids=ids[start:end],
                embeddings=embeddings[start:end],
                documents=documents[start:end],
                metadatas=metadatas[start:end],
            )
            self._advance_progress(task_id, min(EMBEDDING_BATCH_SIZE, len(ids) - start))

    def _advance_progress(self, task_id: str, count: int) -> None:
        """每个 Chroma 批次成功后提交可观察进度。"""

        task = self._get_task(task_id)
        task.progress_current += count
        self.session.commit()

    def _finish_success(self, task_id: str, target_version: int) -> None:
        """两个 Collection 均成功后原子启用当前版本。"""

        self._mark_records_ready(target_version)
        config = self._get_embedding_config()
        config.rebuild_required = 0
        task = self._get_task(task_id)
        task.status = "succeeded"
        task.progress_current = task.progress_total
        task.error_message = None
        task.finished_at = utc_now()
        self.session.commit()

    def _finish_failure(self, task_id: str) -> None:
        """失败时保留事实正文，停用全部索引并记录脱敏任务结果。"""

        self.session.rollback()
        config = self._get_embedding_config()
        config.rebuild_required = 1
        self._mark_indexable_records("failed", error_message=INDEX_FAILURE_MESSAGE)
        task = self._get_task(task_id)
        task.status = "failed"
        task.error_message = INDEX_FAILURE_MESSAGE
        task.finished_at = utc_now()
        self.session.commit()

    def _mark_records_ready(self, target_version: int) -> None:
        """按当前事实正文刷新哈希，并统一标记为目标版本就绪。"""

        entries = self.session.scalars(select(WorldBookEntry)).all()
        for entry in entries:
            entry.content_hash = calculate_content_hash(
                build_worldbook_index_text(
                    name=entry.name,
                    category=entry.category,
                    keywords=self._decode_keywords(entry.keywords_json),
                    content=entry.content,
                )
            )
            entry.index_status = "ready"
            entry.embedding_version = target_version
            entry.last_index_error = None

        snapshots = self.session.scalars(select(SessionWorldBookEntrySnapshot)).all()
        for snapshot in snapshots:
            snapshot.content_hash = calculate_content_hash(
                build_worldbook_index_text(
                    name=snapshot.name,
                    category=snapshot.category,
                    keywords=self._decode_keywords(snapshot.keywords_json),
                    content=snapshot.content,
                )
            )
            snapshot.index_status = "ready"
            snapshot.embedding_version = target_version
            snapshot.last_index_error = None

        memories = self.session.scalars(
            select(LongTermMemory).where(LongTermMemory.status == "active")
        ).all()
        for memory in memories:
            memory.content_hash = calculate_content_hash(memory.content)
            memory.index_status = "ready"
            memory.embedding_version = target_version
            memory.last_index_error = None

    def _mark_indexable_records(
        self,
        status: str,
        *,
        error_message: str | None,
    ) -> None:
        """批量设置正式条目、会话快照和有效记忆的索引状态。"""

        values = {
            "index_status": status,
            "embedding_version": None,
            "last_index_error": error_message,
        }
        self.session.execute(update(WorldBookEntry).values(**values))
        self.session.execute(update(SessionWorldBookEntrySnapshot).values(**values))
        self.session.execute(
            update(LongTermMemory).where(LongTermMemory.status == "active").values(**values)
        )

    def _get_embedding_config(self) -> ModelConfig:
        """读取迁移保证存在的 Embedding 固定配置行。"""

        config = self.session.get(ModelConfig, "embed")
        if config is None:
            raise RuntimeError("缺少 Embedding 模型配置记录")
        return config

    def _get_task(self, task_id: str) -> BackgroundTask:
        """读取本次已提交的任务记录。"""

        task = self.session.get(BackgroundTask, task_id)
        if task is None:
            raise RuntimeError("索引重建任务记录不存在")
        return task

    @staticmethod
    def _decode_keywords(raw_keywords: str) -> list[str]:
        """严格读取数据库中的有序关键词 JSON。"""

        keywords = json.loads(raw_keywords)
        if not isinstance(keywords, list) or any(
            not isinstance(keyword, str) for keyword in keywords
        ):
            raise ValueError("世界书关键词不是字符串数组")
        return keywords

    @staticmethod
    def _already_running_error() -> AppError:
        """构造接口契约规定的任务冲突错误。"""

        return AppError(
            status_code=409,
            code="TASK_ALREADY_RUNNING",
            message="索引重建任务正在运行",
        )
