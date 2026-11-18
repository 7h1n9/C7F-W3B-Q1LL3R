"""Add the arena connection and submission tables.

The arena integration (A scope: a menu page that maintains the connection and
shows status) needs somewhere to keep the bearer token and somewhere to keep the
scorebook, and neither belongs in an existing table.

``arena_connections`` is separate from ``model_configs`` on purpose.  The token is
encrypted with the same key the model API keys use, but an arena credential is not
a model configuration: storing it there would make ``ModelConfigUsage`` report a
reference from a row no run can ever point at, and would turn the settings page's
"model" concept into "model or third-party credential".

``arena_submissions`` exists because the platform does not offer history.  Its
``submit_flag`` answers one call with one verdict and keeps nothing an agent can
read back, so per-model and per-engine claims have to be rebuilt from a local
record.  The unique constraint on (connection, challenge, flag) is the duplicate
guard for the case where several models independently produce the same flag.

Both tables are inspector-guarded so the migration is safe to replay.
"""

import sqlalchemy as sa

from alembic import op

revision = "0046_arena_integration"
down_revision = "0045_model_config_wire_api"
branch_labels = None
depends_on = None

CONNECTIONS = "arena_connections"
SUBMISSIONS = "arena_submissions"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if CONNECTIONS not in existing:
        op.create_table(
            CONNECTIONS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("base_url", sa.String(length=2048), nullable=False),
            sa.Column("encrypted_token", sa.String(length=2048), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("requests_per_minute", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_probe_ok", sa.Boolean(), nullable=True),
            sa.Column("profile_json", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", name="uq_arena_connection_name"),
        )
    if SUBMISSIONS not in existing:
        op.create_table(
            SUBMISSIONS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("connection_id", sa.String(length=36), nullable=False),
            sa.Column("platform_challenge_id", sa.String(length=200), nullable=False),
            sa.Column("challenge_name", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("category", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("attested_flag", sa.String(length=500), nullable=False),
            sa.Column("verdict", sa.String(length=30), nullable=False),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("model_config_id", sa.String(length=36), nullable=True),
            sa.Column("engine_key", sa.String(length=60), nullable=True),
            sa.Column("token_cost", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["connection_id"], [f"{CONNECTIONS}.id"]),
            sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "connection_id",
                "platform_challenge_id",
                "attested_flag",
                name="uq_arena_submission_flag",
            ),
        )
        op.create_index("ix_arena_submissions_connection_id", SUBMISSIONS, ["connection_id"])
        op.create_index(
            "ix_arena_submissions_platform_challenge_id", SUBMISSIONS, ["platform_challenge_id"]
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if SUBMISSIONS in existing:
        op.drop_table(SUBMISSIONS)
    if CONNECTIONS in existing:
        op.drop_table(CONNECTIONS)
