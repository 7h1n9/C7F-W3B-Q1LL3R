"""Store negative Skill triggers used by the structured SkillRouter."""

from alembic import op
import sqlalchemy as sa

revision = "0010_skill_neg_triggers"
down_revision = "0009_skill_catalog_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("skills")}
    if "negative_triggers" not in columns:
        op.add_column("skills", sa.Column("negative_triggers", sa.JSON(), nullable=True))
    op.execute(sa.text("UPDATE skills SET negative_triggers='[]' WHERE negative_triggers IS NULL"))


def downgrade() -> None:
    if "negative_triggers" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("skills")}:
        op.drop_column("skills", "negative_triggers")
