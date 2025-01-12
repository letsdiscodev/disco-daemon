"""0.18.0

Revision ID: 9087484963d4
Revises: 26877eda6774
Create Date: 2025-01-12 01:50:37.649205

"""

import sqlalchemy as sa
from alembic import op

revision = "9087484963d4"
down_revision = "26877eda6774"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("task_id", sa.String(length=32), nullable=True))


def downgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("task_id")
