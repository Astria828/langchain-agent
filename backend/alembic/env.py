"""Alembic 运行环境，统一复用应用的 SQLite 路径与连接配置。"""

from logging.config import fileConfig

from alembic import context
from app.core.config import Settings, get_settings
from app.db.database import create_database_engine, ensure_runtime_directories
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def resolve_settings() -> Settings:
    """允许初始化脚本和测试显式传入隔离的数据目录。"""

    return config.attributes.get("settings") or get_settings()


def run_migrations_offline() -> None:
    """离线生成 SQL，不建立数据库连接。"""

    settings = resolve_settings()
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """使用带统一 PRAGMA 的 Engine 执行正式迁移。"""

    settings = resolve_settings()
    ensure_runtime_directories(settings)
    engine = create_database_engine(settings)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
