"""长期记忆提取、整合、持久化、查询与失效服务。"""

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.chains.memory_chain import (
    build_memory_consolidation_chain,
    build_memory_extraction_chain,
)
from app.core.exceptions import AppError
from app.core.security import unprotect_api_key
from app.db.models import (
    BackgroundTask,
    ChatSession,
    LongTermMemory,
    MemorySource,
    Message,
    MessageBlock,
    ModelConfig,
    SessionContextSnapshot,
    utc_now,
)
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import (
    ChromaRepository,
    calculate_content_hash,
    memory_document_id,
)
from app.repositories.sqlite_repository import SQLiteRepository
from app.retrievers.memory_retriever import MemoryRetriever
from app.schemas.dto import LongTermMemory as LongTermMemoryDto
from app.schemas.dto import (
    MemoryCandidate,
    MemoryConsolidationDecision,
    MemoryConsolidationInput,
    MemoryConsolidationReference,
    MemoryStatus,
    MemoryType,
)

MEMORY_BATCH_SIZE = 10
MEMORY_INDEX_FAILURE_MESSAGE = "长期记忆索引同步失败"
MEMORY_STATUS_TO_DATABASE = {"有效": "active", "已失效": "invalid"}
MEMORY_STATUS_TO_API = {value: key for key, value in MEMORY_STATUS_TO_DATABASE.items()}

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryRound:
    """目标批次中的一个完整用户—助手轮次及其来源消息。"""

    batch_round: int
    absolute_round: int
    user_message_id: str
    assistant_message_id: str
    user_content: str
    assistant_content: str


@dataclass(frozen=True, slots=True)
class MemoryConsolidationPlan:
    """阶段 7.5 可直接消费的候选、决策和有序来源消息。"""

    candidate: MemoryCandidate
    decision: MemoryConsolidationDecision
    source_message_ids: tuple[str, ...]


class MemoryService:
    """编排长期记忆批次读取、候选提取和同角色整合决策。"""

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

    def list_memories(
        self,
        *,
        character_id: str | None = None,
        memory_type: MemoryType | None = None,
        status: MemoryStatus | None = None,
    ) -> list[LongTermMemoryDto]:
        """按角色、类型和 API 状态筛选，并按创建时间倒序返回。"""

        statement = select(LongTermMemory)
        if character_id is not None:
            statement = statement.where(LongTermMemory.character_id == character_id)
        if memory_type is not None:
            statement = statement.where(LongTermMemory.type == memory_type)
        if status is not None:
            statement = statement.where(LongTermMemory.status == MEMORY_STATUS_TO_DATABASE[status])
        memories = self.session.scalars(
            statement.order_by(LongTermMemory.created_at.desc(), LongTermMemory.id.desc())
        ).all()
        return [self._memory_to_dto(memory) for memory in memories]

    def invalidate_memory(self, memory_id: str) -> LongTermMemoryDto:
        """幂等失效长期记忆；SQLite 提交后再清理派生向量。"""

        memory = self.repository.get(LongTermMemory, memory_id)
        if memory is None:
            raise AppError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="长期记忆不存在",
            )
        if memory.status == "invalid":
            return self._memory_to_dto(memory)

        memory.status = "invalid"
        memory.index_status = "stale"
        memory.embedding_version = None
        memory.last_index_error = None
        memory.updated_at = utc_now()
        self.session.commit()

        try:
            self.chroma.delete_memories(ids=[memory_document_id(memory.id)])
        except Exception:
            self._record_memory_cleanup_tasks([memory])
            logger.warning(
                "长期记忆向量清理失败，已记录补偿任务 memory_id=%s",
                memory.id,
                exc_info=True,
            )
        return self._memory_to_dto(memory)

    def load_target_rounds(self, task_id: str) -> list[MemoryRound]:
        """校验记忆任务，并精确返回其目标区间内的十个完整轮次。"""

        task, chat_session = self._load_task_context(task_id)
        return self._load_rounds(chat_session, task.target_version or 0)

    @staticmethod
    def build_extraction_input(rounds: list[MemoryRound]) -> str:
        """以稳定编号和动作/台词语义生成不含内部 ID 的提取输入。"""

        if len(rounds) != MEMORY_BATCH_SIZE:
            raise ValueError("长期记忆提取输入必须包含十个完整轮次")

        lines = ["以下是按时间排序的 10 个完整对话轮次："]
        for memory_round in rounds:
            lines.extend(
                (
                    f"[轮次 {memory_round.batch_round}]",
                    f"用户：{memory_round.user_content}",
                    f"角色：{memory_round.assistant_content}",
                )
            )
        return "\n".join(lines)

    async def extract_and_consolidate(self, task_id: str) -> list[MemoryConsolidationPlan]:
        """提取候选并生成经角色、目标和冲突校验的只读整合计划。"""

        task, chat_session = self._load_task_context(task_id)
        rounds = self._load_rounds(chat_session, task.target_version or 0)
        main_config, main_api_key = self._require_main_model_config()
        extraction_chain = build_memory_extraction_chain(
            gateway=self.gateway,
            base_url=main_config.base_url,
            model=main_config.model_name,
            api_key=main_api_key,
        )
        extraction = await extraction_chain.ainvoke(self.build_extraction_input(rounds))
        self._validate_unique_candidates(extraction.candidates)
        if not extraction.candidates:
            return []

        plans_by_index: dict[int, MemoryConsolidationPlan] = {}
        candidates_for_similarity: list[tuple[int, MemoryCandidate]] = []
        for index, candidate in enumerate(extraction.candidates):
            exact_memory = self._find_exact_memory(chat_session.character_id, candidate)
            if exact_memory is None:
                candidates_for_similarity.append((index, candidate))
                continue
            plans_by_index[index] = MemoryConsolidationPlan(
                candidate=candidate,
                decision=MemoryConsolidationDecision(
                    action="deduplicate",
                    target_memory_id=exact_memory.id,
                ),
                source_message_ids=self._source_message_ids(candidate, rounds),
            )

        if candidates_for_similarity:
            embedding_config, embedding_api_key = self._require_embedding_config()
            target_embedding_version = embedding_config.config_version
            embeddings = await self.gateway.create_embeddings(
                base_url=embedding_config.base_url,
                model=embedding_config.model_name,
                api_key=embedding_api_key,
                texts=[candidate.content for _, candidate in candidates_for_similarity],
                expected_dimension=embedding_config.vector_dimension,
            )
            self._assert_embedding_version_current(target_embedding_version)
            consolidation_chain = build_memory_consolidation_chain(
                gateway=self.gateway,
                base_url=main_config.base_url,
                model=main_config.model_name,
                api_key=main_api_key,
            )
            retriever = MemoryRetriever(self.session, self.chroma)

            for (index, candidate), embedding in zip(
                candidates_for_similarity,
                embeddings,
                strict=True,
            ):
                similar_memories = retriever.retrieve(
                    character_id=chat_session.character_id,
                    query_embedding=embedding,
                    embedding_version=target_embedding_version,
                )
                consolidation_input = MemoryConsolidationInput(
                    candidate=candidate,
                    existing_memories=[
                        MemoryConsolidationReference(
                            id=memory.id,
                            type=memory.type,
                            content=memory.content,
                            importance=memory.importance,
                        )
                        for memory in similar_memories
                    ],
                )
                decision = await consolidation_chain.ainvoke(consolidation_input)
                self.session.expire_all()
                self._assert_embedding_version_current(target_embedding_version)
                self._validate_decision_target(
                    character_id=chat_session.character_id,
                    allowed_memories=similar_memories,
                    decision=decision,
                )
                plans_by_index[index] = MemoryConsolidationPlan(
                    candidate=candidate,
                    decision=decision,
                    source_message_ids=self._source_message_ids(candidate, rounds),
                )

        plans = [plans_by_index[index] for index in range(len(extraction.candidates))]
        self._validate_plan_conflicts(plans)
        return plans

    def has_persisted_batch(self, task_id: str) -> bool:
        """使用目标十轮的来源关系判断本批次 SQLite 计划是否已经提交。"""

        task, chat_session = self._load_task_context(task_id)
        rounds = self._load_rounds(chat_session, task.target_version or 0)
        return bool(self._load_batch_memories(self._all_source_message_ids(rounds)))

    def persist_consolidation_plans(
        self,
        task_id: str,
        plans: list[MemoryConsolidationPlan],
    ) -> None:
        """在一个 SQLite 事务中应用全部非忽略动作和来源关系。"""

        task, chat_session = self._load_task_context(task_id)
        target_round = task.target_version or 0
        rounds = self._load_rounds(chat_session, target_round)
        allowed_source_ids = set(self._all_source_message_ids(rounds))
        if self._load_batch_memories(tuple(allowed_source_ids)):
            return

        source_label = (
            f"会话「{chat_session.title}」第 "
            f"{target_round - MEMORY_BATCH_SIZE + 1}–{target_round} 轮"
        )
        for plan in plans:
            if not set(plan.source_message_ids).issubset(allowed_source_ids):
                raise self._invalid_task_error("长期记忆候选引用了目标批次之外的消息")
            if plan.decision.action == "ignore":
                continue

            memory = self._apply_plan(chat_session, plan, source_label)
            self.repository.flush()
            self._append_memory_sources(memory.id, plan.source_message_ids)
        self.session.commit()

    async def synchronize_and_finish(self, task_id: str) -> None:
        """同步本批次派生向量，并原子推进会话进度和任务成功状态。"""

        task, chat_session = self._load_task_context(task_id)
        rounds = self._load_rounds(chat_session, task.target_version or 0)
        source_ids = self._all_source_message_ids(rounds)
        batch_memories = self._load_batch_memories(source_ids)
        current_embedding_config = self.repository.get(ModelConfig, "embed")
        current_embedding_version = (
            current_embedding_config.config_version if current_embedding_config is not None else 0
        )
        active_to_sync = [
            memory
            for memory in batch_memories
            if memory.status == "active"
            and (
                memory.index_status != "ready"
                or memory.embedding_version != current_embedding_version
                or calculate_content_hash(memory.content) != memory.content_hash
            )
        ]
        invalid_to_delete = [memory for memory in batch_memories if memory.status == "invalid"]

        target_embedding_version: int | None = None
        sync_snapshot: dict[str, tuple[str, str, str, str, int]] = {}
        if active_to_sync:
            embedding_config, embedding_api_key = self._require_embedding_config()
            target_embedding_version = embedding_config.config_version
            sync_snapshot = {
                memory.id: (
                    memory.character_id,
                    memory.type,
                    memory.content,
                    memory.content_hash,
                    memory.memory_version,
                )
                for memory in active_to_sync
            }
            embeddings = await self.gateway.create_embeddings(
                base_url=embedding_config.base_url,
                model=embedding_config.model_name,
                api_key=embedding_api_key,
                texts=[memory.content for memory in active_to_sync],
                expected_dimension=embedding_config.vector_dimension,
            )
            self._assert_embedding_version_current(target_embedding_version)
            current_memories = self._assert_sync_sources_unchanged(sync_snapshot)
            self.chroma.upsert_memories(
                ids=[memory_document_id(memory.id) for memory in current_memories],
                embeddings=embeddings,
                documents=[memory.content for memory in current_memories],
                metadatas=[
                    {
                        "memoryId": memory.id,
                        "characterId": memory.character_id,
                        "status": "active",
                        "memoryType": memory.type,
                        "embeddingVersion": target_embedding_version,
                        "contentHash": memory.content_hash,
                    }
                    for memory in current_memories
                ],
            )

        if invalid_to_delete:
            try:
                self.chroma.delete_memories(
                    ids=[memory_document_id(memory.id) for memory in invalid_to_delete]
                )
            except Exception as exc:
                self._record_memory_cleanup_tasks(invalid_to_delete)
                raise AppError(
                    status_code=500,
                    code="INDEX_OPERATION_FAILED",
                    message="失效长期记忆向量删除失败",
                ) from exc

        self.session.expire_all()
        task, chat_session = self._load_task_context(task_id)
        if target_embedding_version is not None:
            self._assert_embedding_version_current(target_embedding_version)
            current_memories = self._assert_sync_sources_unchanged(sync_snapshot)
            for memory in current_memories:
                memory.index_status = "ready"
                memory.embedding_version = target_embedding_version
                memory.last_index_error = None
                memory.updated_at = utc_now()
        self._finish_memory_cleanup_tasks(invalid_to_delete)
        chat_session.consolidated_round = task.target_version or 0
        task.status = "succeeded"
        task.progress_current = MEMORY_BATCH_SIZE
        task.progress_total = MEMORY_BATCH_SIZE
        task.error_message = None
        task.finished_at = utc_now()
        self._enqueue_next_batch_if_due(chat_session)
        self.session.commit()

    def mark_incomplete_batch_failed(self, task_id: str) -> None:
        """把本批次尚未就绪的有效记忆标记失败，供同一任务后续修复。"""

        task, chat_session = self._load_task_context(task_id)
        rounds = self._load_rounds(chat_session, task.target_version or 0)
        for memory in self._load_batch_memories(self._all_source_message_ids(rounds)):
            if memory.status == "active" and memory.index_status != "ready":
                memory.index_status = "failed"
                memory.embedding_version = None
                memory.last_index_error = MEMORY_INDEX_FAILURE_MESSAGE
                memory.updated_at = utc_now()

    def _load_task_context(self, task_id: str) -> tuple[BackgroundTask, ChatSession]:
        """校验任务、会话、角色快照和目标轮次之间的一致性。"""

        task = self.repository.get(BackgroundTask, task_id)
        if task is None or task.task_type != "memory_consolidation":
            raise AppError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="长期记忆整理任务不存在",
            )
        if task.scope_id is None or task.target_version is None:
            raise self._invalid_task_error("长期记忆整理任务缺少会话或目标轮次")

        chat_session = self.repository.get(ChatSession, task.scope_id)
        if chat_session is None:
            raise self._invalid_task_error("长期记忆整理任务对应的会话不存在")
        context_snapshot = self.session.scalar(
            select(SessionContextSnapshot).where(
                SessionContextSnapshot.session_id == chat_session.id
            )
        )
        if (
            context_snapshot is None
            or context_snapshot.character_source_id != chat_session.character_id
        ):
            raise self._invalid_task_error("会话角色绑定与上下文快照不一致")
        if task.target_version > chat_session.round_count:
            raise self._invalid_task_error("长期记忆整理目标轮次超出当前会话轮次")
        if task.target_version != chat_session.consolidated_round + MEMORY_BATCH_SIZE:
            raise self._invalid_task_error("长期记忆整理任务不是下一个连续十轮区间")
        return task, chat_session

    @staticmethod
    def _memory_to_dto(memory: LongTermMemory) -> LongTermMemoryDto:
        """把数据库英文状态映射为稳定的中文 API DTO。"""

        return LongTermMemoryDto(
            id=memory.id,
            character_id=memory.character_id,
            type=memory.type,
            content=memory.content,
            importance=memory.importance,
            status=MEMORY_STATUS_TO_API[memory.status],
            created_at=memory.created_at,
            source_label=memory.source_label,
        )

    def _apply_plan(
        self,
        chat_session: ChatSession,
        plan: MemoryConsolidationPlan,
        source_label: str,
    ) -> LongTermMemory:
        """应用一条已校验计划，但不在此处提交事务。"""

        decision = plan.decision
        if decision.action == "create":
            if decision.content is None or decision.importance is None:
                raise RuntimeError("create 决策缺少最终记忆内容")
            exact_memory = self._find_exact_memory_content(
                character_id=chat_session.character_id,
                memory_type=plan.candidate.type,
                content=decision.content,
            )
            if exact_memory is not None:
                return exact_memory
            return self.repository.add(
                LongTermMemory(
                    character_id=chat_session.character_id,
                    type=plan.candidate.type,
                    content=decision.content,
                    importance=decision.importance,
                    status="active",
                    source_label=source_label,
                    memory_version=1,
                    index_status="pending",
                    embedding_version=None,
                    content_hash=calculate_content_hash(decision.content),
                    last_index_error=None,
                )
            )

        target = self._get_active_plan_target(chat_session.character_id, decision)
        if decision.action == "deduplicate":
            return target
        if decision.action in {"merge", "update"}:
            if decision.content is None or decision.importance is None:
                raise RuntimeError("更新类决策缺少最终记忆内容")
            target.content = decision.content
            target.importance = decision.importance
            target.memory_version += 1
            target.content_hash = calculate_content_hash(decision.content)
            target.index_status = "pending"
            target.embedding_version = None
            target.last_index_error = None
            target.updated_at = utc_now()
            return target
        if decision.action == "invalidate":
            target.status = "invalid"
            target.index_status = "stale"
            target.embedding_version = None
            target.last_index_error = None
            target.updated_at = utc_now()
            return target
        raise RuntimeError("未处理的长期记忆整合动作")

    def _get_active_plan_target(
        self,
        character_id: str,
        decision: MemoryConsolidationDecision,
    ) -> LongTermMemory:
        """在写库前再次确认计划目标仍属于当前角色且有效。"""

        if decision.target_memory_id is None:
            raise RuntimeError("整合动作缺少目标记忆 ID")
        target = self.repository.get(LongTermMemory, decision.target_memory_id)
        if target is None or target.character_id != character_id or target.status != "active":
            raise AppError(
                status_code=409,
                code="MEMORY_PLAN_STALE",
                message="长期记忆整合目标在写入前已发生变化",
            )
        return target

    def _append_memory_sources(self, memory_id: str, source_message_ids: tuple[str, ...]) -> None:
        """按候选来源顺序追加尚未存在的消息关系。"""

        last_order = self.session.scalar(
            select(func.max(MemorySource.source_order)).where(MemorySource.memory_id == memory_id)
        )
        next_order = (last_order if last_order is not None else -1) + 1
        for message_id in source_message_ids:
            if self.repository.get(MemorySource, (memory_id, message_id)) is not None:
                continue
            self.repository.add(
                MemorySource(
                    memory_id=memory_id,
                    message_id=message_id,
                    source_order=next_order,
                )
            )
            next_order += 1

    def _load_batch_memories(self, source_message_ids: tuple[str, ...]) -> list[LongTermMemory]:
        """读取与目标十轮任一来源消息关联的全部记忆。"""

        if not source_message_ids:
            return []
        return list(
            self.session.scalars(
                select(LongTermMemory)
                .join(MemorySource, MemorySource.memory_id == LongTermMemory.id)
                .where(MemorySource.message_id.in_(source_message_ids))
                .distinct()
                .order_by(LongTermMemory.id)
            ).all()
        )

    def _assert_sync_sources_unchanged(
        self,
        expected: dict[str, tuple[str, str, str, str, int]],
    ) -> list[LongTermMemory]:
        """外部向量调用前后确认记忆正文、角色、类型和业务版本未变化。"""

        self.session.expire_all()
        memories = [
            memory
            for memory_id in expected
            if (memory := self.repository.get(LongTermMemory, memory_id)) is not None
        ]
        current = {
            memory.id: (
                memory.character_id,
                memory.type,
                memory.content,
                memory.content_hash,
                memory.memory_version,
            )
            for memory in memories
            if memory.status == "active"
        }
        if current != expected:
            raise AppError(
                status_code=409,
                code="MEMORY_PLAN_STALE",
                message="长期记忆在索引同步期间发生变化",
            )
        return memories

    def _enqueue_next_batch_if_due(self, chat_session: ChatSession) -> None:
        """在当前批次完成事务中创建同会话下一个积压十轮任务。"""

        if chat_session.round_count - chat_session.consolidated_round < MEMORY_BATCH_SIZE:
            return
        target_round = chat_session.consolidated_round + MEMORY_BATCH_SIZE
        existing_task_id = self.session.scalar(
            select(BackgroundTask.id).where(
                BackgroundTask.task_type == "memory_consolidation",
                BackgroundTask.scope_id == chat_session.id,
                BackgroundTask.target_version == target_round,
            )
        )
        if existing_task_id is not None:
            return
        self.repository.add(
            BackgroundTask(
                task_type="memory_consolidation",
                status="pending",
                scope_id=chat_session.id,
                target_version=target_round,
                progress_current=0,
                progress_total=MEMORY_BATCH_SIZE,
            )
        )

    def _record_memory_cleanup_tasks(self, memories: list[LongTermMemory]) -> None:
        """为删除失败的确定记忆 ID 创建不重复的向量清理补偿任务。"""

        for memory in memories:
            document_id = memory_document_id(memory.id)
            existing_task_id = self.session.scalar(
                select(BackgroundTask.id).where(
                    BackgroundTask.task_type == "vector_cleanup",
                    BackgroundTask.scope_id.in_((memory.id, document_id)),
                    BackgroundTask.status.in_(("pending", "running")),
                )
            )
            if existing_task_id is not None:
                continue
            self.repository.add(
                BackgroundTask(
                    task_type="vector_cleanup",
                    status="pending",
                    scope_id=document_id,
                    progress_current=0,
                    progress_total=1,
                    error_message="长期记忆向量待清理",
                )
            )
        self.session.commit()

    def _finish_memory_cleanup_tasks(self, memories: list[LongTermMemory]) -> None:
        """确定向量已删除后关闭对应的活动补偿任务。"""

        document_ids = [memory_document_id(memory.id) for memory in memories]
        legacy_memory_ids = [memory.id for memory in memories]
        if not document_ids:
            return
        cleanup_tasks = self.session.scalars(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "vector_cleanup",
                BackgroundTask.scope_id.in_((*document_ids, *legacy_memory_ids)),
                BackgroundTask.status.in_(("pending", "running")),
            )
        ).all()
        finished_at = utc_now()
        for cleanup_task in cleanup_tasks:
            cleanup_task.status = "succeeded"
            cleanup_task.progress_current = cleanup_task.progress_total
            cleanup_task.error_message = None
            cleanup_task.finished_at = finished_at

    def _load_rounds(self, chat_session: ChatSession, target_round: int) -> list[MemoryRound]:
        """按不可变轮次编号读取目标区间，不受已整理历史删除影响。"""

        first_absolute_round = target_round - MEMORY_BATCH_SIZE + 1
        messages = self.session.scalars(
            select(Message)
            .where(
                Message.session_id == chat_session.id,
                Message.role.in_(("user", "assistant")),
                Message.turn_number.between(first_absolute_round, target_round),
            )
            .order_by(Message.turn_number, Message.position, Message.id)
        ).all()
        messages_by_turn: dict[int, dict[str, Message]] = {}
        for message in messages:
            if message.turn_number is None:
                continue
            messages_by_turn.setdefault(message.turn_number, {})[message.role] = message

        selected: list[tuple[int, Message, Message]] = []
        for absolute_round in range(first_absolute_round, target_round + 1):
            pair = messages_by_turn.get(absolute_round, {})
            user_message = pair.get("user")
            assistant_message = pair.get("assistant")
            if user_message is None or assistant_message is None:
                raise self._invalid_task_error("会话完整消息轮次缺少目标整理区间")
            selected.append((absolute_round, user_message, assistant_message))

        return [
            MemoryRound(
                batch_round=batch_round,
                absolute_round=absolute_round,
                user_message_id=user_message.id,
                assistant_message_id=assistant_message.id,
                user_content=self._render_message(user_message),
                assistant_content=self._render_message(assistant_message),
            )
            for batch_round, (absolute_round, user_message, assistant_message) in enumerate(
                selected,
                start=1,
            )
        ]

    def _render_message(self, message: Message) -> str:
        """按连续块顺序还原用户正文或带动作/台词标记的角色回复。"""

        blocks = self.session.scalars(
            select(MessageBlock)
            .where(MessageBlock.message_id == message.id)
            .order_by(MessageBlock.sequence, MessageBlock.id)
        ).all()
        if not blocks or [block.sequence for block in blocks] != list(range(len(blocks))):
            raise self._invalid_task_error("长期记忆来源消息的内容块不完整")
        if message.role == "user":
            if len(blocks) != 1 or blocks[0].type != "dialogue":
                raise self._invalid_task_error("长期记忆来源中的用户消息格式无效")
            return blocks[0].content
        return "\n".join(
            f"[{'动作' if block.type == 'action' else '台词'}] {block.content}" for block in blocks
        )

    def _require_main_model_config(self) -> tuple[ModelConfig, str]:
        """读取可用的主模型配置和密钥。"""

        config = self.repository.get(ModelConfig, "main")
        if config is None or not config.base_url or not config.model_name or not config.secret_ref:
            raise AppError(
                status_code=503,
                code="MODEL_NOT_CONFIGURED",
                message="主模型尚未配置",
            )
        try:
            api_key = unprotect_api_key(config.secret_ref)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise AppError(
                status_code=503,
                code="MODEL_NOT_CONFIGURED",
                message="主模型密钥不可用",
            ) from exc
        return config, api_key

    def _require_embedding_config(self) -> tuple[ModelConfig, str]:
        """读取当前可检索的 Embedding 配置和密钥。"""

        config = self.repository.get(ModelConfig, "embed")
        if config is None:
            raise RuntimeError("缺少 Embedding 模型配置记录")
        if config.rebuild_required:
            raise AppError(
                status_code=503,
                code="INDEX_REBUILD_REQUIRED",
                message="Embedding 索引需要完成重建后才能整理长期记忆",
            )
        if (
            not config.base_url
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
        try:
            api_key = unprotect_api_key(config.secret_ref)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise AppError(
                status_code=503,
                code="MODEL_NOT_CONFIGURED",
                message="Embedding 模型密钥不可用",
            ) from exc
        return config, api_key

    def _assert_embedding_version_current(self, target_version: int) -> None:
        """拒绝使用外部调用期间已经变化或待重建的向量空间。"""

        self.session.expire_all()
        current_config = self.repository.get(ModelConfig, "embed")
        if (
            current_config is None
            or current_config.rebuild_required
            or current_config.config_version != target_version
        ):
            raise AppError(
                status_code=409,
                code="INDEX_VERSION_CHANGED",
                message="Embedding 配置在长期记忆整理期间发生变化",
            )

    def _find_exact_memory(
        self,
        character_id: str,
        candidate: MemoryCandidate,
    ) -> LongTermMemory | None:
        """只在当前角色有效记忆中查找正文和哈希均一致的精确重复。"""

        return self._find_exact_memory_content(
            character_id=character_id,
            memory_type=candidate.type,
            content=candidate.content,
        )

    def _find_exact_memory_content(
        self,
        *,
        character_id: str,
        memory_type: str,
        content: str,
    ) -> LongTermMemory | None:
        """按角色、类型、正文和哈希查找稳定的有效精确重复。"""

        content_hash = calculate_content_hash(content)
        matches = self.session.scalars(
            select(LongTermMemory)
            .where(
                LongTermMemory.character_id == character_id,
                LongTermMemory.type == memory_type,
                LongTermMemory.status == "active",
                LongTermMemory.content_hash == content_hash,
            )
            .order_by(LongTermMemory.created_at, LongTermMemory.id)
        ).all()
        return next(
            (
                memory
                for memory in matches
                if memory.content == content
                and calculate_content_hash(memory.content) == memory.content_hash
            ),
            None,
        )

    @staticmethod
    def _validate_unique_candidates(candidates: list[MemoryCandidate]) -> None:
        """拒绝提取结果内部的完全重复候选，避免后续产生两次新增。"""

        keys = [
            (candidate.type, calculate_content_hash(candidate.content)) for candidate in candidates
        ]
        if len(keys) != len(set(keys)):
            raise AppError(
                status_code=502,
                code="MODEL_OUTPUT_INVALID",
                message="长期记忆提取结果包含重复候选",
            )

    def _validate_decision_target(
        self,
        *,
        character_id: str,
        allowed_memories: list[LongTermMemory],
        decision: MemoryConsolidationDecision,
    ) -> None:
        """模型调用后再次确认目标仍有效、同角色且来自本次相似集合。"""

        if decision.target_memory_id is None:
            return
        allowed_ids = {memory.id for memory in allowed_memories}
        target = self.repository.get(LongTermMemory, decision.target_memory_id)
        if (
            decision.target_memory_id not in allowed_ids
            or target is None
            or target.character_id != character_id
            or target.status != "active"
        ):
            raise AppError(
                status_code=502,
                code="MODEL_OUTPUT_INVALID",
                message="长期记忆整合结果引用了无效目标",
            )

    @staticmethod
    def _validate_plan_conflicts(plans: list[MemoryConsolidationPlan]) -> None:
        """同一已有记忆只允许被多次去重，不允许同批次出现冲突修改。"""

        target_actions: dict[str, str] = {}
        for plan in plans:
            target_id = plan.decision.target_memory_id
            if target_id is None:
                continue
            previous_action = target_actions.get(target_id)
            if previous_action is not None and (
                previous_action != "deduplicate" or plan.decision.action != "deduplicate"
            ):
                raise AppError(
                    status_code=502,
                    code="MODEL_OUTPUT_INVALID",
                    message="长期记忆整合结果包含冲突动作",
                )
            target_actions[target_id] = plan.decision.action

    @staticmethod
    def _source_message_ids(
        candidate: MemoryCandidate,
        rounds: list[MemoryRound],
    ) -> tuple[str, ...]:
        """按候选来源轮次依次映射用户和助手消息 ID。"""

        round_by_number = {memory_round.batch_round: memory_round for memory_round in rounds}
        source_ids: list[str] = []
        for source_round in candidate.source_rounds:
            memory_round = round_by_number[source_round]
            source_ids.extend((memory_round.user_message_id, memory_round.assistant_message_id))
        return tuple(source_ids)

    @staticmethod
    def _all_source_message_ids(rounds: list[MemoryRound]) -> tuple[str, ...]:
        """按轮次顺序展开目标批次全部用户和助手消息 ID。"""

        return tuple(
            message_id
            for memory_round in rounds
            for message_id in (
                memory_round.user_message_id,
                memory_round.assistant_message_id,
            )
        )

    @staticmethod
    def _invalid_task_error(message: str) -> AppError:
        """构造不会泄露对话正文的任务一致性错误。"""

        return AppError(
            status_code=409,
            code="MEMORY_TASK_INVALID",
            message=message,
        )
