"""向量清理补偿任务的路由、失败和启动恢复测试。"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.core.config import Settings
from app.db.database import create_database_engine, create_session_factory
from app.db.models import BackgroundTask
from app.main import create_app
from app.repositories.chroma_repository import (
    memory_document_id,
    session_entry_document_id,
    worldbook_entry_document_id,
)
from app.tasks.vector_cleanup_task import (
    SESSION_ENTRY_CLEANUP_MESSAGE,
    VectorCleanupTask,
    recover_interrupted_vector_cleanup_tasks,
    run_pending_vector_cleanup_tasks,
)
from fastapi.testclient import TestClient

from scripts.init_db import initialize_database


class RecordingChroma:
    """记录两个 Collection 的确定 ID 删除，并支持受控失败。"""

    def __init__(self) -> None:
        self.worldbook_ids: list[str] = []
        self.memory_ids: list[str] = []
        self.memory_error: Exception | None = None

    def delete_worldbook(self, *, ids: list[str]) -> None:
        """记录世界书条目或会话快照向量删除。"""

        self.worldbook_ids.extend(ids)

    def delete_memories(self, *, ids: list[str]) -> None:
        """记录长期记忆向量删除或抛出受控错误。"""

        self.memory_ids.extend(ids)
        if self.memory_error is not None:
            raise self.memory_error


def create_database(tmp_path: Path):
    """创建隔离的清理任务测试数据库。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    return settings, engine, create_session_factory(engine)


def test_cleanup_runner_routes_collections_and_normalizes_legacy_task(
    tmp_path: Path,
) -> None:
    """确定前缀路由到正确 Collection，旧裸 ID 任务先规范化再执行。"""

    settings, engine, session_factory = create_database(tmp_path)
    try:
        with session_factory() as session:
            tasks = [
                BackgroundTask(
                    task_type="vector_cleanup",
                    status="pending",
                    scope_id=worldbook_entry_document_id("entry-1"),
                    progress_total=1,
                ),
                BackgroundTask(
                    task_type="vector_cleanup",
                    status="pending",
                    scope_id=memory_document_id("memory-1"),
                    progress_total=1,
                ),
                BackgroundTask(
                    task_type="vector_cleanup",
                    status="pending",
                    scope_id="snapshot-legacy",
                    progress_total=1,
                    error_message=SESSION_ENTRY_CLEANUP_MESSAGE,
                ),
            ]
            session.add_all(tasks)
            session.commit()
            task_ids = [task.id for task in tasks]

        chroma = RecordingChroma()
        asyncio.run(run_pending_vector_cleanup_tasks(settings, chroma))

        # 同毫秒创建的任务由随机 UUID 次级排序，业务只要求目标完整且路由正确。
        assert sorted(chroma.worldbook_ids) == sorted(
            [
                worldbook_entry_document_id("entry-1"),
                session_entry_document_id("snapshot-legacy"),
            ]
        )
        assert chroma.memory_ids == [memory_document_id("memory-1")]
        with session_factory() as session:
            refreshed = [session.get(BackgroundTask, task_id) for task_id in task_ids]
            assert all(
                task is not None and task.status == "succeeded" for task in refreshed
            )
            assert refreshed[2] is not None
            assert refreshed[2].scope_id == session_entry_document_id("snapshot-legacy")
    finally:
        engine.dispose()


def test_cleanup_failure_is_sanitized_and_interrupted_task_can_resume(
    tmp_path: Path,
) -> None:
    """外部删除失败只保存脱敏状态，中断任务恢复后可安全重试。"""

    _settings, engine, session_factory = create_database(tmp_path)
    try:
        with session_factory() as session:
            failed = BackgroundTask(
                task_type="vector_cleanup",
                status="pending",
                scope_id=memory_document_id("memory-failed"),
                progress_total=1,
            )
            interrupted = BackgroundTask(
                task_type="vector_cleanup",
                status="running",
                scope_id="snapshot-interrupted",
                progress_total=1,
                error_message=SESSION_ENTRY_CLEANUP_MESSAGE,
            )
            session.add_all([failed, interrupted])
            session.commit()

            chroma = RecordingChroma()
            chroma.memory_error = RuntimeError("secret vector backend detail")
            assert VectorCleanupTask(session, chroma).run(failed.id) is False
            session.expire_all()
            failed_task = session.get(BackgroundTask, failed.id)
            assert failed_task is not None and failed_task.status == "failed"
            assert failed_task.error_message == "向量清理失败"

            assert recover_interrupted_vector_cleanup_tasks(session) == 1
            assert (
                VectorCleanupTask(session, RecordingChroma()).run(interrupted.id)
                is True
            )
            session.expire_all()
            resumed = session.get(BackgroundTask, interrupted.id)
            assert resumed is not None and resumed.status == "succeeded"
            assert resumed.scope_id == session_entry_document_id("snapshot-interrupted")
    finally:
        engine.dispose()


def test_application_startup_recovers_and_schedules_vector_cleanup(
    tmp_path: Path,
) -> None:
    """应用启动恢复 running 清理任务，并调用独立清理调度器。"""

    settings, engine, session_factory = create_database(tmp_path)
    try:
        with session_factory() as session:
            task = BackgroundTask(
                task_type="vector_cleanup",
                status="running",
                scope_id=memory_document_id("memory-startup"),
                progress_total=1,
            )
            session.add(task)
            session.commit()
            task_id = task.id

        with (
            patch("app.main.run_pending_memory_tasks", new_callable=AsyncMock),
            patch(
                "app.main.run_pending_vector_cleanup_tasks",
                new_callable=AsyncMock,
            ) as cleanup_runner,
        ):
            with TestClient(create_app(settings)) as client:
                assert client.get("/api/health").status_code == 200
            cleanup_runner.assert_awaited_once_with(settings)

        with session_factory() as session:
            recovered = session.get(BackgroundTask, task_id)
            assert recovered is not None and recovered.status == "pending"
            assert recovered.started_at is None
    finally:
        engine.dispose()
