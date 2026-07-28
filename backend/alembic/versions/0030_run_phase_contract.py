"""Keep solver phase independent from lifecycle status."""

import sqlalchemy as sa
from alembic import op


revision = "0030_run_phase_contract"
down_revision = "0029_network_enforcement_manifest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(sa.text("UPDATE solve_runs SET current_phase='INTAKE' WHERE current_phase IN ('CREATED','PREPARING','ANALYZING','PLANNING','EXECUTING','EVALUATING','WAITING_USER','WAITING_CONFIGURATION','PAUSED_DEPLOYMENT','PAUSED_RECOVERY','PAUSED_CHECKPOINT','PAUSED_RATE_LIMIT','FAILED_ENGINE','FAILED_RUNNER','COMPLETED_SOLVED','COMPLETED_UNSOLVED')"))


def downgrade() -> None:
    pass
