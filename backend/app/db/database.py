"""SQLite 连接、PRAGMA 配置与 SQLAlchemy Session 管理。"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings


def ensure_runtime_directories(settings: Settings) -> None:
    """创建规范约定的运行目录，不覆盖其中已有内容。"""

    data_dir = settings.resolved_data_dir
    for path in (data_dir, settings.chroma_dir, settings.logs_dir, settings.exports_dir):
        path.mkdir(parents=True, exist_ok=True)


def create_database_engine(settings: Settings | None = None) -> Engine:
    """创建启用外键、WAL 和忙等待的 SQLite Engine。"""

    resolved_settings = settings or get_settings()
    ensure_runtime_directories(resolved_settings)
    engine = create_engine(
        resolved_settings.database_url,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        """每次建立底层连接时应用 SQLite 一致性配置。"""

        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA busy_timeout = 5000")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建不自动提交的 Session 工厂，事务由服务层控制。"""

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """延迟创建并复用正式环境的 Engine 与 Session 工厂。"""

    return create_session_factory(create_database_engine())


def get_db_session() -> Iterator[Session]:
    """为单次请求提供独立 Session；不在依赖层提交事务。"""

    with get_session_factory()() as session:
        yield session
