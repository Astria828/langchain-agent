"""执行并恢复 SQLite 中记录的 Chroma 向量清理补偿任务。"""

import logging

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.database import create_database_engine, create_session_factory
from app.db.models import BackgroundTask, utc_now
from app.repositories.chroma_repository import (
    ChromaRepository,
    memory_document_id,
    session_entry_document_id,
    worldbook_entry_document_id,
)

logger = logging.getLogger(__name__)

WORLD_BOOK_CLEANUP_MESSAGE = "世界书向量待清理"
SESSION_ENTRY_CLEANUP_MESSAGE = "会话世界书快照向量待清理"
MEMORY_CLEANUP_MESSAGE = "长期记忆向量待清理"
VECTOR_CLEANUP_FAILURE_MESSAGE = "向量清理失败"


def recover_interrupted_vector_cleanup_tasks(session: Session) -> int:
    """应用启动时把中断的清理任务恢复为可重新领取状态。"""

    task_ids = session.scalars(
        select(BackgroundTask.id).where(
            BackgroundTask.task_type == "vector_cleanup",
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
            started_at=None,
            finished_at=None,
        )
    )
    session.commit()
    logger.warning(
        "已恢复中断的向量清理任务",
        extra={
            "event": "vector_cleanup_tasks_recovered",
            "business_ids": {"count": len(task_ids)},
        },
    )
    return len(task_ids)


class VectorCleanupTask:
    """按确定文档 ID 将残留向量从正确的 Chroma Collection 删除。"""

    def __init__(self, session: Session, chroma_repository: ChromaRepository) -> None:
        self.session = session
        self.chroma = chroma_repository

    def run(self, task_id: str) -> bool:
        """原子领取一个 pending 清理任务，并保存成功或脱敏失败状态。"""

        if not self._claim(task_id):
            return False
        task = self.session.get(BackgroundTask, task_id)
        if task is None:
            return False
        try:
            document_id = self._normalize_document_id(task)
            task.scope_id = document_id
            task.error_message = None
            self.session.commit()

            if document_id.startswith("memory:"):
                self.chroma.delete_memories(ids=[document_id])
            else:
                self.chroma.delete_worldbook(ids=[document_id])
        except Exception as exc:
            self._finish_failure(task_id)
            logger.exception(
                "向量清理任务失败",
                exc_info=exc,
                extra={
                    "event": "vector_cleanup_failed",
                    "business_ids": {"taskId": task_id},
                },
            )
            return False

        task = self.session.get(BackgroundTask, task_id)
        if task is None:
            return False
        task.status = "succeeded"
        task.progress_current = task.progress_total
        task.error_message = None
        task.finished_at = utc_now()
        self.session.commit()
        logger.info(
            "向量清理任务完成",
            extra={
                "event": "vector_cleanup_completed",
                "business_ids": {"taskId": task_id},
            },
        )
        return True

    def _claim(self, task_id: str) -> bool:
        """通过条件更新避免多个调度器重复执行同一个清理任务。"""

        result = self.session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.task_type == "vector_cleanup",
                BackgroundTask.status == "pending",
            )
            .values(
                status="running",
                progress_current=0,
                started_at=utc_now(),
                finished_at=None,
            )
        )
        self.session.commit()
        return result.rowcount == 1

    def _finish_failure(self, task_id: str) -> None:
        """清理失败时保留确定目标，供后续恢复或人工排查。"""

        self.session.rollback()
        task = self.session.get(BackgroundTask, task_id)
        if task is None:
            return
        task.status = "failed"
        task.error_message = VECTOR_CLEANUP_FAILURE_MESSAGE
        task.finished_at = utc_now()
        self.session.commit()

    @staticmethod
    def _normalize_document_id(task: BackgroundTask) -> str:
        """兼容旧任务的裸业务 ID，并统一转换成带类型前缀的文档 ID。"""

        scope_id = task.scope_id or ""
        if scope_id.startswith(("worldbook-entry:", "session-entry:", "memory:")):
            return scope_id
        if not scope_id:
            raise ValueError("向量清理任务缺少目标 ID")
        if task.error_message == WORLD_BOOK_CLEANUP_MESSAGE:
            return worldbook_entry_document_id(scope_id)
        if task.error_message == SESSION_ENTRY_CLEANUP_MESSAGE:
            return session_entry_document_id(scope_id)
        if task.error_message == MEMORY_CLEANUP_MESSAGE:
            return memory_document_id(scope_id)
        raise ValueError("向量清理任务目标类型未知")


async def run_pending_vector_cleanup_tasks(
    settings: Settings,
    chroma_repository: ChromaRepository | None = None,
) -> None:
    """使用独立 Session 串行执行当前全部 pending 向量清理任务。"""

    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    resolved_chroma = chroma_repository
    try:
        with session_factory() as session:
            while True:
                task_id = session.scalar(
                    select(BackgroundTask.id)
                    .where(
                        BackgroundTask.task_type == "vector_cleanup",
                        BackgroundTask.status == "pending",
                    )
                    .order_by(BackgroundTask.created_at, BackgroundTask.id)
                )
                if task_id is None:
                    break
                if resolved_chroma is None:
                    resolved_chroma = ChromaRepository(settings)
                VectorCleanupTask(session, resolved_chroma).run(task_id)
                session.expire_all()
    except Exception:
        logger.exception(
            "向量清理后台调度器异常",
            extra={"event": "vector_cleanup_scheduler_failed", "business_ids": {}},
        )
    finally:
        engine.dispose()
