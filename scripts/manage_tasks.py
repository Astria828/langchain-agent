"""本地后台任务管理入口。"""

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.database import create_database_engine, create_session_factory
from app.db.models import BackgroundTask
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import ChromaRepository
from app.tasks.memory_task import MemoryTask, retry_failed_memory_task
from sqlalchemy import select


def list_failed_memory_tasks(
    settings: Settings | None = None,
) -> list[tuple[str, str | None, int | None, str | None]]:
    """按创建时间返回失败记忆任务的 ID、会话、目标轮次和脱敏错误。"""

    resolved_settings = settings or Settings()
    if not resolved_settings.database_path.exists():
        return []
    engine = create_database_engine(resolved_settings)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            tasks = session.scalars(
                select(BackgroundTask)
                .where(
                    BackgroundTask.task_type == "memory_consolidation",
                    BackgroundTask.status == "failed",
                )
                .order_by(BackgroundTask.created_at, BackgroundTask.id)
            ).all()
            return [
                (
                    task.id,
                    task.scope_id,
                    task.target_version,
                    task.error_message,
                )
                for task in tasks
            ]
    finally:
        engine.dispose()


async def retry_memory_task_once(
    task_id: str,
    settings: Settings | None = None,
    gateway: ModelGateway | None = None,
    chroma_repository: ChromaRepository | None = None,
) -> str:
    """把指定 failed 记忆任务重置并立即执行一次，返回最终状态。"""

    resolved_settings = settings or Settings()
    if not resolved_settings.database_path.exists():
        return "database_missing"

    engine = create_database_engine(resolved_settings)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            if not retry_failed_memory_task(session, task_id):
                task = session.get(BackgroundTask, task_id)
                if task is None or task.task_type != "memory_consolidation":
                    return "not_found"
                return task.status

            resolved_gateway = gateway or ModelGateway()
            resolved_chroma = chroma_repository or ChromaRepository(resolved_settings)
            await MemoryTask(
                session,
                resolved_gateway,
                resolved_chroma,
            ).run(task_id)
            session.expire_all()
            task = session.get(BackgroundTask, task_id)
            return task.status if task is not None else "not_found"
    finally:
        engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    """构造稳定、可扩展的本地任务管理命令。"""

    parser = argparse.ArgumentParser(description="管理织境后台任务")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "list-failed-memory",
        help="列出全部 failed 长期记忆整理任务",
    )
    retry_memory = subparsers.add_parser(
        "retry-memory",
        help="重试一个 failed 长期记忆整理任务",
    )
    retry_memory.add_argument("task_id", help="background_tasks 表中的任务 ID")
    return parser


def main() -> int:
    """解析命令并返回适合 PowerShell 检查的退出码。"""

    args = build_parser().parse_args()
    settings = Settings()
    configure_logging(settings)
    if args.command == "list-failed-memory":
        if not settings.database_path.exists():
            print(f"数据库不存在，请先初始化：{settings.database_path}")
            return 1
        tasks = list_failed_memory_tasks(settings)
        if not tasks:
            print("当前没有失败的长期记忆任务")
            return 0
        for task_id, session_id, target_round, error_message in tasks:
            print(
                f"{task_id}\tsession={session_id}\ttarget={target_round}"
                f"\terror={error_message or '-'}"
            )
        return 0

    final_status = asyncio.run(retry_memory_task_once(args.task_id, settings))
    if final_status == "succeeded":
        print(f"长期记忆任务重试成功：{args.task_id}")
        return 0
    if final_status == "database_missing":
        print(f"数据库不存在，请先初始化：{settings.database_path}")
        return 1
    if final_status == "not_found":
        print(f"未找到长期记忆任务：{args.task_id}")
        return 1
    if final_status != "failed":
        print(f"任务当前状态为 {final_status}，只有 failed 任务可以重试")
        return 1
    print(f"长期记忆任务重试失败，请查看日志：{args.task_id}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
