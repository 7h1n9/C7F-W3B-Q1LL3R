"""Repair the required timestamp on compaction leases."""

import sqlalchemy as sa

from alembic import op


revision = "0023_compaction_lease_updated_at"
down_revision = "0022_durable_methodology_hints"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "compaction_leases" in sa.inspect(op.get_bind()).get_table_names() and "updated_at" not in _columns("compaction_leases"):
        op.add_column(
            "compaction_leases",
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.execute(
            sa.text(
                "UPDATE compaction_leases SET updated_at = created_at "
                "WHERE updated_at IS NULL"
            )
        )
        op.alter_column("compaction_leases", "updated_at", nullable=False)


def downgrade() -> None:
    if "compaction_leases" in sa.inspect(op.get_bind()).get_table_names() and "updated_at" in _columns("compaction_leases"):
        op.drop_column("compaction_leases", "updated_at")
