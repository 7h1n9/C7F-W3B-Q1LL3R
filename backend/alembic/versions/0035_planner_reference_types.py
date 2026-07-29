"""Separate Planner VerifiedFact and EvidenceLedger references."""

import sqlalchemy as sa
from alembic import op


revision = "0035_planner_reference_types"
down_revision = "0034_approved_action_compilation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("planner_proposals")}
    if "input_evidence_ids_json" not in columns:
        op.add_column("planner_proposals", sa.Column("input_evidence_ids_json", sa.JSON(), nullable=True))
    op.execute(sa.text("UPDATE planner_proposals SET input_evidence_ids_json = '[]' WHERE input_evidence_ids_json IS NULL"))
    op.alter_column("planner_proposals", "input_evidence_ids_json", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    if "input_evidence_ids_json" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("planner_proposals")}:
        op.drop_column("planner_proposals", "input_evidence_ids_json")
