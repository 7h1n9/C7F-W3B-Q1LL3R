"""Add Skill catalog scope for separating Web CTF specialists from general skills."""

from alembic import op
import sqlalchemy as sa

revision = "0009_skill_catalog_scope"
down_revision = "0008_provider_agent_turns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("skills")}
    if "catalog_scope" not in columns:
        op.add_column("skills", sa.Column("catalog_scope", sa.String(30), nullable=True))
    op.execute(sa.text("UPDATE skills SET catalog_scope='WEB_CTF' WHERE catalog_scope IS NULL"))
    unrelated = (
        "'security-awareness-training','incident-response','cloud-security-audit',"
        "'container-security-testing','mobile-app-security-testing','network-penetration-testing','vulnerability-assessment'"
    )
    op.execute(sa.text(f"UPDATE skills SET enabled=0, catalog_scope='GENERAL_SECURITY' WHERE name IN ({unrelated})"))
    op.execute(sa.text("UPDATE skills SET required_tools='[]', recommended_tools='[]' WHERE name='ctf-solver-core'"))


def downgrade() -> None:
    if "catalog_scope" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("skills")}:
        op.drop_column("skills", "catalog_scope")
