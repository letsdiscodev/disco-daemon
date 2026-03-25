"""Add no_cache column to deployments

Revision ID: a1b2c3d4e5f6
Revises: d8adabff2804
Create Date: 2026-03-25 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "d8adabff2804"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "no_cache",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("no_cache")
