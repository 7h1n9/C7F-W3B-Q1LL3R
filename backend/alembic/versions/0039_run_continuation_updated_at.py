"""Give durable continuations a database-generated updated_at value."""

import sqlalchemy as sa
from alembic import op


revision = "0039_run_continuation_updated_at"
down_revision = "0038_run_continuations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "run_continuations" not in inspector.get_table_names():
        return
    op.alter_column(
        "run_continuations",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "run_continuations" not in inspector.get_table_names():
        return
    op.alter_column(
        "run_continuations",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        nullable=False,
        server_default=None,
    )
