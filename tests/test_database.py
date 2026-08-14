"""阶段 1A 的 SQLite 连接、迁移和 Schema 一致性测试。"""

from pathlib import Path

import pytest
from alembic import command
from app.core.config import Settings
from app.db.database import create_database_engine
from app.db.models import Base
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from scripts.init_db import build_alembic_config, initialize_database

EXPECTED_TABLES = {
    "background_tasks",
    "characters",
    "long_term_memories",
    "memory_sources",
    "message_blocks",
    "messages",
    "model_configs",
    "session_context_snapshots",
    "session_world_book_entry_snapshots",
    "sessions",
    "user_identities",
    "world_book_entries",
    "world_books",
}


def isolated_settings(tmp_path: Path) -> Settings:
    """构造不接触正式 data/app.db 的测试配置。"""

    return Settings(environment="test", data_dir=tmp_path)


def test_initial_migration_is_idempotent_and_seeds_required_rows(
    tmp_path: Path,
) -> None:
    """空目录可升级到 head，重复执行不改写初始数据。"""

    settings = isolated_settings(tmp_path)

    assert initialize_database(settings) == tmp_path / "app.db"
    initialize_database(settings)
    command.check(build_alembic_config(settings))

    engine = create_database_engine(settings)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == EXPECTED_TABLES | {"alembic_version"}

    with engine.connect() as connection:
        identity = connection.execute(
            text("SELECT singleton_key, name, persona_name, bio FROM user_identities")
        ).one()
        assert identity == ("current", "用户", "默认身份", "")

        model_groups = (
            connection.execute(
                text("SELECT group_name FROM model_configs ORDER BY group_name")
            )
            .scalars()
            .all()
        )
        assert model_groups == ["embed", "main"]

    assert (tmp_path / "chroma").is_dir()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "exports").is_dir()
    engine.dispose()


def test_every_connection_enables_required_sqlite_pragmas(tmp_path: Path) -> None:
    """每个连接都启用外键、WAL 和 5000ms 忙等待。"""

    settings = isolated_settings(tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)

    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000

    engine.dispose()


def test_migration_schema_matches_sqlalchemy_metadata(tmp_path: Path) -> None:
    """迁移结果和 SQLAlchemy 模型保持相同的表与列边界。"""

    settings = isolated_settings(tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    inspector = inspect(engine)

    assert set(Base.metadata.tables) == EXPECTED_TABLES
    for table_name, table in Base.metadata.tables.items():
        migration_columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        model_columns = {column.name for column in table.columns}
        assert migration_columns == model_columns

    engine.dispose()


def test_initial_migration_can_downgrade_to_base(tmp_path: Path) -> None:
    """首个 revision 可完整回退，便于开发阶段安全撤销。"""

    settings = isolated_settings(tmp_path)
    initialize_database(settings)
    command.downgrade(build_alembic_config(settings), "base")

    engine = create_database_engine(settings)
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    engine.dispose()


def test_turn_number_migration_backfills_only_complete_rounds(tmp_path: Path) -> None:
    """历史开场白和孤立用户消息不编号，完整用户—助手轮次共享编号。"""

    settings = isolated_settings(tmp_path)
    config = build_alembic_config(settings)
    command.upgrade(config, "ba3fc7114bef")
    engine = create_database_engine(settings)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO characters "
                "(id, name, introduction, system_prompt, dialogue_examples_json, "
                "created_at, updated_at) VALUES "
                "('character-1', '艾拉', '', '', '[]', 'now', 'now')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sessions "
                "(id, title, character_id, round_count, consolidated_round, summary, "
                "created_at, updated_at) VALUES "
                "('session-1', '测试', 'character-1', 1, 0, '', 'now', 'now')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO messages "
                "(id, session_id, position, role, retrieved_entries_json, created_at) VALUES "
                "('opening', 'session-1', 0, 'assistant', '[]', 'now'), "
                "('user-1', 'session-1', 1, 'user', NULL, 'now'), "
                "('assistant-1', 'session-1', 2, 'assistant', '[]', 'now'), "
                "('orphan-user', 'session-1', 3, 'user', NULL, 'now')"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(settings)
    with engine.connect() as connection:
        assigned = connection.execute(
            text("SELECT id, turn_number FROM messages ORDER BY position")
        ).all()
        assert assigned == [
            ("opening", None),
            ("user-1", 1),
            ("assistant-1", 1),
            ("orphan-user", None),
        ]
    engine.dispose()


def test_foreign_keys_and_check_constraints_are_enforced(tmp_path: Path) -> None:
    """引用边界与轮次约束由 SQLite 本身兜底。"""

    settings = isolated_settings(tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)

    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO sessions "
                    "(id, title, character_id, round_count, consolidated_round, "
                    "summary, created_at, updated_at) "
                    "VALUES ('session-invalid', '测试', 'missing', 0, 0, '', 'now', 'now')"
                )
            )
        connection.rollback()

        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO model_configs "
                    "(group_name, updated_at) VALUES ('other', 'now')"
                )
            )

    engine.dispose()
