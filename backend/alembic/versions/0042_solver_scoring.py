"""Add solver prediction and run scoring tables."""

import sqlalchemy as sa
from alembic import op


revision = "0042_solver_scoring"
down_revision = "0041_attack_strategy_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "challenge_predictions" not in inspector.get_table_names():
        op.create_table(
            "challenge_predictions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("challenge_id", sa.String(length=36), nullable=False),
            sa.Column("prediction_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING"),
            sa.Column("fingerprint", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("predicted_solve_seconds", sa.Integer(), nullable=True),
            sa.Column("predicted_tokens", sa.Integer(), nullable=True),
            sa.Column("predicted_tool_calls", sa.Integer(), nullable=True),
            sa.Column("difficulty", sa.String(length=30), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("rationale_zh", sa.Text(), nullable=True),
            sa.Column("model", sa.String(length=120), nullable=True),
            sa.Column("usage_json", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
            sa.Column("error_code", sa.String(length=120), nullable=True),
            sa.ForeignKeyConstraint(["challenge_id"], ["challenges.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("challenge_id", name="uq_challenge_prediction_challenge"),
        )
        op.create_index("ix_challenge_predictions_challenge_id", "challenge_predictions", ["challenge_id"])
    if "run_scores" not in inspector.get_table_names():
        op.create_table(
            "run_scores",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("challenge_id", sa.String(length=36), nullable=False),
            sa.Column("prediction_snapshot_json", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
            sa.Column("actual_seconds", sa.Float(), nullable=True),
            sa.Column("actual_tokens", sa.Integer(), nullable=True),
            sa.Column("solved", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("time_ratio", sa.Float(), nullable=True),
            sa.Column("token_ratio", sa.Float(), nullable=True),
            sa.Column("time_points", sa.Float(), nullable=True),
            sa.Column("token_points", sa.Float(), nullable=True),
            sa.Column("solved_points", sa.Float(), nullable=True),
            sa.Column("total_score", sa.Float(), nullable=True),
            sa.Column("formula_version", sa.String(length=20), nullable=False, server_default="1.0"),
            sa.Column("score_status", sa.String(length=30), nullable=False, server_default="PENDING"),
            sa.Column("error_code", sa.String(length=120), nullable=True),
            sa.ForeignKeyConstraint(["challenge_id"], ["challenges.id"]),
            sa.ForeignKeyConstraint(["run_id"], ["solve_runs.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", name="uq_run_score_run"),
        )
        op.create_index("ix_run_scores_run_id", "run_scores", ["run_id"])
        op.create_index("ix_run_scores_challenge_id", "run_scores", ["challenge_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "run_scores" in inspector.get_table_names():
        op.drop_table("run_scores")
    if "challenge_predictions" in inspector.get_table_names():
        op.drop_table("challenge_predictions")
