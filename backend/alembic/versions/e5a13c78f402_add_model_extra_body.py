"""add_model_extra_body

Revision ID: e5a13c78f402
Revises: b8d4f2a61c07
Create Date: 2026-08-23 01:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5a13c78f402"
down_revision: str | None = "b8d4f2a61c07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """应用本次 Schema 变更。"""

    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "extra_body_json",
                sa.Text(),
                nullable=False,
                server_default=sa.text("''"),
            )
        )


def downgrade() -> None:
    """回退本次 Schema 变更。"""

    with op.batch_alter_table("model_configs", schema=None) as batch_op:
        batch_op.drop_column("extra_body_json")
