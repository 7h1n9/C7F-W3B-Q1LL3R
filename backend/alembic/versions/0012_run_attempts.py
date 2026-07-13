"""Track independent execution attempts for a Run."""
from alembic import op
import sqlalchemy as sa

revision = "0012_run_attempts"
down_revision = "0011_learned_skill_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "run_attempts" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table("run_attempts", sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("attempt_number", sa.Integer(), nullable=False), sa.Column("engine_type", sa.String(40), nullable=False), sa.Column("model_config_id", sa.String(36), sa.ForeignKey("model_configs.id")), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)), sa.Column("status", sa.String(40), nullable=False), sa.Column("error_code", sa.String(100)), sa.Column("agent_steps", sa.Integer(), nullable=False), sa.Column("tool_calls", sa.Integer(), nullable=False), sa.Column("input_tokens", sa.Integer(), nullable=False), sa.Column("output_tokens", sa.Integer(), nullable=False))


def downgrade() -> None:
    op.drop_table("run_attempts")
