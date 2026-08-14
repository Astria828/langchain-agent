"""add_message_turn_number

Revision ID: c3f8a91d2e74
Revises: ba3fc7114bef
Create Date: 2026-08-14 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f8a91d2e74"
down_revision: str | None = "ba3fc7114bef"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加稳定轮次编号，并为现有完整用户—助手轮次回填编号。"""

    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("turn_number", sa.Integer(), nullable=True))

    connection = op.get_bind()
    sessions = connection.execute(
        sa.text("SELECT id, round_count FROM sessions ORDER BY id")
    ).mappings()
    for chat_session in sessions:
        messages = connection.execute(
            sa.text(
                "SELECT id, role FROM messages "
                "WHERE session_id = :session_id ORDER BY position, id"
            ),
            {"session_id": chat_session["id"]},
        ).mappings()
        pending_user_id: str | None = None
        turn_number = 0
        for message in messages:
            if message["role"] == "user":
                pending_user_id = message["id"]
                continue
            if message["role"] != "assistant" or pending_user_id is None:
                continue
            turn_number += 1
            connection.execute(
                sa.text(
                    "UPDATE messages SET turn_number = :turn_number "
                    "WHERE id IN (:user_id, :assistant_id)"
                ),
                {
                    "turn_number": turn_number,
                    "user_id": pending_user_id,
                    "assistant_id": message["id"],
                },
            )
            pending_user_id = None

        if turn_number != chat_session["round_count"]:
            raise RuntimeError(
                f"会话 {chat_session['id']} 的完整消息轮次与 round_count 不一致"
            )

    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_messages_turn_number",
            "turn_number IS NULL OR turn_number > 0",
        )
        batch_op.create_index(
            "ux_messages_turn_role",
            ["session_id", "turn_number", "role"],
            unique=True,
            sqlite_where=sa.text("turn_number IS NOT NULL"),
        )


def downgrade() -> None:
    """移除稳定轮次编号。"""

    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_index("ux_messages_turn_role")
        batch_op.drop_constraint("ck_messages_turn_number", type_="check")
        batch_op.drop_column("turn_number")
