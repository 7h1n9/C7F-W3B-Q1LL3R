"""add solver methodology state, role snapshots, and skill metadata

Revision ID: 0005_solver_methodology_flow
Revises: 0004_reconcile_chat_summary
Create Date: 2026-07-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_solver_methodology_flow"
down_revision = "0004_reconcile_chat_summary"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    with op.batch_alter_table("solve_runs") as batch:
        if not _has_column("solve_runs", "role_name"):
            batch.add_column(sa.Column("role_name", sa.String(length=120)))
        if not _has_column("solve_runs", "role_version"):
            batch.add_column(sa.Column("role_version", sa.String(length=40)))
        if not _has_column("solve_runs", "role_snapshot_json"):
            batch.add_column(sa.Column("role_snapshot_json", sa.JSON(), nullable=True))
    if _has_column("solve_runs", "role_snapshot_json"):
        op.execute(
            "UPDATE solve_runs SET role_snapshot_json = JSON_OBJECT() WHERE role_snapshot_json IS NULL"
        )
        with op.batch_alter_table("solve_runs") as batch:
            batch.alter_column("role_snapshot_json", existing_type=sa.JSON(), nullable=False)
    with op.batch_alter_table("skills") as batch:
        if not _has_column("skills", "skill_kind"):
            batch.add_column(
                sa.Column(
                    "skill_kind",
                    sa.String(length=20),
                    nullable=False,
                    server_default="SPECIALIST",
                )
            )
        if not _has_column("skills", "activation_mode"):
            batch.add_column(
                sa.Column(
                    "activation_mode",
                    sa.String(length=20),
                    nullable=False,
                    server_default="MANUAL",
                )
            )
        for column in ("triggers", "prerequisites", "required_tools", "recommended_tools", "forbidden_tools", "ctf_phases"):
            if not _has_column("skills", column):
                batch.add_column(sa.Column(column, sa.JSON(), nullable=True))
    for column in ("triggers", "prerequisites", "required_tools", "recommended_tools", "forbidden_tools", "ctf_phases"):
        if _has_column("skills", column):
            op.execute(f"UPDATE skills SET {column} = JSON_ARRAY() WHERE {column} IS NULL")
    with op.batch_alter_table("skills") as batch:
        for column in ("triggers", "prerequisites", "required_tools", "recommended_tools", "forbidden_tools", "ctf_phases"):
            if _has_column("skills", column):
                batch.alter_column(column, existing_type=sa.JSON(), nullable=False)
    op.create_table(
        "solver_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
        sa.Column("current_phase", sa.String(length=80), nullable=False),
        sa.Column("confirmed_facts_json", sa.JSON(), nullable=False),
        sa.Column("rejected_paths_json", sa.JSON(), nullable=False),
        sa.Column("active_hypotheses_json", sa.JSON(), nullable=False),
        sa.Column("action_fingerprints_json", sa.JSON(), nullable=False),
        sa.Column("active_skill_ids_json", sa.JSON(), nullable=False),
        sa.Column("no_progress_count", sa.Integer(), nullable=False),
        sa.Column("last_progress_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", name="uq_solver_state_run"),
    )
    op.create_index("ix_solver_states_run_id", "solver_states", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_solver_states_run_id", table_name="solver_states")
    op.drop_table("solver_states")
    with op.batch_alter_table("skills") as batch:
        for column in (
            "ctf_phases",
            "forbidden_tools",
            "recommended_tools",
            "required_tools",
            "prerequisites",
            "triggers",
            "activation_mode",
            "skill_kind",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("solve_runs") as batch:
        batch.drop_column("role_snapshot_json")
        batch.drop_column("role_version")
        batch.drop_column("role_name")
