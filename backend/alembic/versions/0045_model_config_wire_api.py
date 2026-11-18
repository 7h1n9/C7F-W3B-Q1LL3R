"""Persist the OpenAI wire dialect a model configuration speaks.

``wire_api`` was accepted by the write schema but had no column, so the operator's
choice was silently dropped on save.  That went unnoticed because the value was
always derivable: Codex is pinned to Responses and everything else was assumed to
be Chat Completions.  A relay that serves only one of the two dialects makes that
assumption wrong -- pointing a Chat-only relay at ``/responses`` returns a 502 and
pointing a Responses-only relay at ``/chat/completions`` returns a 404 -- so the
choice has to survive a round trip.

The column is nullable: an existing row keeps ``NULL`` and the application
normalises that to ``chat_completions``, which is exactly the behaviour those rows
had before this migration.  Inspector-guarded so it is safe to replay.
"""

import sqlalchemy as sa

from alembic import op

revision = "0045_model_config_wire_api"
down_revision = "0044_model_config_deletable"
branch_labels = None
depends_on = None

TABLE = "model_configs"


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    if "wire_api" in _columns(TABLE):
        return
    op.add_column(TABLE, sa.Column("wire_api", sa.String(30), nullable=True))


def downgrade() -> None:
    if "wire_api" not in _columns(TABLE):
        return
    op.drop_column(TABLE, "wire_api")
