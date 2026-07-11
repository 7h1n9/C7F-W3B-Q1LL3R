"""Reconcile the conversation summary column on upgraded installations."""

import sqlalchemy as sa

from alembic import op

revision = "0004_reconcile_chat_summary"
down_revision = "0003_skills_chat_traffic"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("solve_runs", "conversation_summary"):
        op.add_column("solve_runs", sa.Column("conversation_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("solve_runs", "conversation_summary"):
        op.drop_column("solve_runs", "conversation_summary")
