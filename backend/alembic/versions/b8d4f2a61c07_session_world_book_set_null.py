"""session_world_book_set_null

Revision ID: b8d4f2a61c07
Revises: a7c1e93b5d20
Create Date: 2026-08-22 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d4f2a61c07"
down_revision: str | None = "a7c1e93b5d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sessions_table(world_book_ondelete: str) -> sa.Table:
    """按指定的世界书外键动作描述 sessions 表结构。

    SQLite 不能原地修改外键，batch_alter_table 需要一份完整的表定义来重建。
    """

    return sa.Table(
        "sessions",
        sa.MetaData(),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("character_id", sa.Text(), nullable=False),
        sa.Column("world_book_id", sa.Text(), nullable=True),
        sa.Column("round_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "consolidated_round", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("round_count >= 0", name="ck_sessions_round_count"),
        sa.CheckConstraint("consolidated_round >= 0", name="ck_sessions_consolidated_round"),
        sa.CheckConstraint(
            "consolidated_round <= round_count", name="ck_sessions_consolidated_not_ahead"
        ),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["world_book_id"], ["world_books.id"], ondelete=world_book_ondelete
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_sessions_updated_at", "updated_at"),
        sa.Index("ix_sessions_character", "character_id", "updated_at"),
        sa.Index("ix_sessions_world_book", "world_book_id"),
    )


def _rebuild_sessions(world_book_ondelete: str) -> None:
    """以指定外键动作重建 sessions 表。

    SQLite 重建表要先 DROP 原表，此时若外键仍在生效，messages 会被
    ON DELETE CASCADE 连带删光。重建期间关闭约束，完成后校验完整性再恢复。
    """

    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table(
            "sessions",
            copy_from=_sessions_table(world_book_ondelete),
            recreate="always",
        ):
            pass
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"重建 sessions 后存在外键完整性问题：{violations[:5]}")
    finally:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    """把会话对世界书的外键从 RESTRICT 改为 SET NULL。

    会话不再冻结世界书快照，删除世界书应让相关会话降级为无世界书注入，
    而不是拒绝删除；这条规则原先只写在服务层，现在由 schema 自己表达。
    """

    _rebuild_sessions("SET NULL")


def downgrade() -> None:
    """恢复为 RESTRICT；被引用的世界书将重新变为不可删除。"""

    _rebuild_sessions("RESTRICT")
