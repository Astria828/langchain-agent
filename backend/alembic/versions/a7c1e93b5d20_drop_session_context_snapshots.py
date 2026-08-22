"""drop_session_context_snapshots

Revision ID: a7c1e93b5d20
Revises: c3f8a91d2e74
Create Date: 2026-08-22 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c1e93b5d20"
down_revision: str | None = "c3f8a91d2e74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """删除会话上下文与世界书条目快照表，会话改为实时读取绑定源。"""

    # 先按既有 session-entry: 前缀登记补偿任务，再删表，
    # 让现有 vector_cleanup 机制照常删除这些孤立快照向量。
    connection = op.get_bind()
    snapshot_ids = connection.execute(
        sa.text("SELECT id FROM session_world_book_entry_snapshots ORDER BY id")
    ).scalars()
    for snapshot_id in snapshot_ids:
        connection.execute(
            sa.text(
                "INSERT INTO background_tasks "
                "(id, task_type, status, scope_id, progress_current, progress_total, "
                " error_message, created_at) "
                "VALUES (lower(hex(randomblob(16))), 'vector_cleanup', 'pending', "
                " :scope_id, 0, 1, '会话世界书快照向量待清理', "
                " strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
            ),
            {"scope_id": f"session-entry:{snapshot_id}"},
        )

    op.drop_table("session_world_book_entry_snapshots")
    op.drop_table("session_context_snapshots")


def downgrade() -> None:
    """重建空的快照表结构；历史快照内容不可恢复。"""

    op.create_table(
        "session_context_snapshots",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("identity_source_id", sa.Text(), nullable=False),
        sa.Column("identity_name", sa.Text(), nullable=False),
        sa.Column("identity_persona_name", sa.Text(), nullable=False),
        sa.Column("identity_bio", sa.Text(), nullable=False),
        sa.Column("character_source_id", sa.Text(), nullable=False),
        sa.Column("character_name", sa.Text(), nullable=False),
        sa.Column("character_introduction", sa.Text(), nullable=False),
        sa.Column("character_system_prompt", sa.Text(), nullable=False),
        sa.Column("character_dialogue_examples_json", sa.Text(), nullable=False),
        sa.Column("world_book_source_id", sa.Text(), nullable=True),
        sa.Column("world_book_name", sa.Text(), nullable=True),
        sa.Column("world_book_raw_content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
    )
    op.create_table(
        "session_world_book_entry_snapshots",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("context_snapshot_id", sa.Text(), nullable=False),
        sa.Column("source_entry_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("keywords_json", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("resident", sa.Integer(), nullable=False),
        sa.Column("index_status", sa.Text(), nullable=False),
        sa.Column("embedding_version", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("last_index_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("resident IN (0, 1)", name="ck_session_entry_snapshots_resident"),
        sa.CheckConstraint(
            "index_status IN ('pending', 'ready', 'stale', 'failed')",
            name="ck_session_entry_snapshots_index_status",
        ),
        sa.ForeignKeyConstraint(
            ["context_snapshot_id"], ["session_context_snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_session_entry_snapshot_source",
        "session_world_book_entry_snapshots",
        ["context_snapshot_id", "source_entry_id"],
        unique=True,
    )
    op.create_index(
        "ix_session_entry_snapshot_lookup",
        "session_world_book_entry_snapshots",
        ["context_snapshot_id", "resident", "position"],
    )
