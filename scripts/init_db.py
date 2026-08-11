"""创建运行目录并将 SQLite 数据库升级到最新 Alembic revision。"""

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings
from app.db.database import ensure_runtime_directories
from app.repositories.chroma_repository import ChromaRepository


def build_alembic_config(settings: Settings) -> Config:
    """构造显式绑定当前数据目录的 Alembic 配置。"""

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    return config


def initialize_database(settings: Settings | None = None) -> Path:
    """保留现有数据并幂等升级 SQLite、初始化双 Collection。"""

    resolved_settings = settings or Settings()
    ensure_runtime_directories(resolved_settings)
    command.upgrade(build_alembic_config(resolved_settings), "head")
    ChromaRepository(resolved_settings).initialize_collections()
    return resolved_settings.database_path


def main() -> None:
    """命令行入口。"""

    database_path = initialize_database()
    print(f"本地存储初始化完成：{database_path}")


if __name__ == "__main__":
    main()
