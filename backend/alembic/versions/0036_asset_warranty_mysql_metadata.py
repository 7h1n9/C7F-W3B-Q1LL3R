"""Declare the asset-warranty target DBMS and request contract."""

import sqlalchemy as sa
from alembic import op


revision = "0036_asset_warranty_mysql_metadata"
down_revision = "0035_planner_reference_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE challenges
            SET metadata_json = JSON_SET(
                COALESCE(metadata_json, JSON_OBJECT()),
                '$.dbms', 'mysql',
                '$.endpoint', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.endpoint')), '/api/warranty/check'),
                '$.method', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.method')), 'POST'),
                '$.content_type', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.content_type')), 'application/json'),
                '$.fields', COALESCE(JSON_EXTRACT(metadata_json, '$.fields'), JSON_ARRAY('asset_no', 'department')),
                '$.control_values', COALESCE(JSON_EXTRACT(metadata_json, '$.control_values'), JSON_OBJECT('asset_no', 'PC-2026-013', 'department', 'OPS'))
            )
            WHERE JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.adapter')) = 'asset_warranty'
            """
        )
    )


def downgrade() -> None:
    # The metadata is an explicit compatibility contract once written. Keep it
    # on downgrade so old Runs cannot silently route to a different DBMS.
    pass
