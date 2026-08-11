"""在 FastAPI 进程内执行、恢复并串行推进长期记忆整理任务。"""

import logging

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.database import create_database_engine, create_session_factory
from app.db.models import BackgroundTask, ChatSession, utc_now
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import ChromaRepository
from app.services.memory_service import MEMORY_BATCH_SIZE, MemoryService

logger = logging.getLogger(__name__)
MEMORY_TASK_FAILURE_MESSAGE = "长期记忆整理失败"


def recover_interrupted_memory_tasks(session: Session) -> int:
    """启动时把遗留 running 任务恢复为可重新领取的 pending 状态。"""

    task_ids = session.scalars(
        select(BackgroundTask.id).where(
            BackgroundTask.task_type == "memory_consolidation",
            BackgroundTask.status == "running",
        )
    ).all()
    if not task_ids:
        return 0
    session.execute(
        update(BackgroundTask)
        .where(BackgroundTask.id.in_(task_ids))
        .values(
            status="pending",
            error_message=None,
            started_at=None,
            finished_at=None,
        )
    )
    session.commit()
    logger.warning("已恢复 %s 个中断的长期记忆整理任务", len(task_ids))
    return len(task_ids)


def retry_failed_memory_task(session: Session, task_id: str) -> bool:
    """把一个失败任务重置为 pending，保留其来源关系供幂等修复。"""

    result = session.execute(
        update(BackgroundTask)
        .where(
            BackgroundTask.id == task_id,
            BackgroundTask.task_type == "memory_consolidation",
            BackgroundTask.status == "failed",
        )
        .values(
            status="pending",
            progress_current=0,
            error_message=None,
            started_at=None,
            finished_at=None,
        )
    )
    session.commit()
    return result.rowcount == 1


class MemoryTask:
    """领取并执行一个可恢复的长期记忆整理任务。"""

    def __init__(
        self,
        session: Session,
        gateway: ModelGateway,
        chroma_repository: ChromaRepository,
    ) -> None:
        self.session = session
        self.gateway = gateway
        self.chroma = chroma_repository

    async def run(self, task_id: str) -> bool:
        """原子领取 pending 任务，完成整理或持久化脱敏失败状态。"""

        if not self._claim(task_id):
            return False
        service = MemoryService(self.session, self.gateway, self.chroma)
        try:
            if not service.has_persisted_batch(task_id):
                plans = await service.extract_and_consolidate(task_id)
                service.persist_consolidation_plans(task_id, plans)
            await service.synchronize_and_finish(task_id)
            return True
        except Exception as exc:
            self._finish_failure(task_id, service)
            logger.exception("长期记忆整理任务失败 task_id=%s", task_id, exc_info=exc)
            return False

    def _claim(self, task_id: str) -> bool:
        """通过条件更新保证多个调度器只能有一个领取同一任务。"""

        result = self.session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.task_type == "memory_consolidation",
                BackgroundTask.status == "pending",
            )
            .values(
                status="running",
                progress_current=0,
                progress_total=MEMORY_BATCH_SIZE,
                error_message=None,
                started_at=utc_now(),
                finished_at=None,
            )
        )
        self.session.commit()
        return result.rowcount == 1

    def _finish_failure(self, task_id: str, service: MemoryService) -> None:
        """保留已提交事实数据，不推进轮次，并记录可安全重试的失败状态。"""

        self.session.rollback()
        try:
            service.mark_incomplete_batch_failed(task_id)
        except Exception:
            self.session.rollback()
        task = self.session.get(BackgroundTask, task_id)
        if task is None:
            return
        task.status = "failed"
        task.error_message = MEMORY_TASK_FAILURE_MESSAGE
        task.finished_at = utc_now()
        self.session.commit()

async def run_pending_memory_tasks(
    settings: Settings,
    gateway: ModelGateway | None = None,
    chroma_repository: ChromaRepository | None = None,
) -> None:
    """使用独立 Session 按会话进度循环执行所有当前可运行的 pending 任务。"""

    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    resolved_gateway = gateway or ModelGateway()
    resolved_chroma = chroma_repository
    try:
        with session_factory() as session:
            while True:
                task_id = session.scalar(
                    select(BackgroundTask.id)
                    .join(ChatSession, ChatSession.id == BackgroundTask.scope_id)
                    .where(
                        BackgroundTask.task_type == "memory_consolidation",
                        BackgroundTask.status == "pending",
                        BackgroundTask.target_version
                        == ChatSession.consolidated_round + MEMORY_BATCH_SIZE,
                    )
                    .order_by(
                        BackgroundTask.created_at,
                        BackgroundTask.scope_id,
                        BackgroundTask.target_version,
                    )
                )
                if task_id is None:
                    break
                if resolved_chroma is None:
                    resolved_chroma = ChromaRepository(settings)
                await MemoryTask(session, resolved_gateway, resolved_chroma).run(task_id)
                session.expire_all()
    except Exception:
        logger.exception("长期记忆后台调度器异常")
    finally:
        engine.dispose()
