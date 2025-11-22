"""0.27.0

Revision ID: b0b4edb3672a
Revises: 9087484963d4
Create Date: 2025-11-22 22:32:42.378628

"""

import sqlalchemy as sa
from alembic import op

revision = "b0b4edb3672a"
down_revision = "9087484963d4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("pending_github_apps", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("setup_url", sa.Unicode(length=1000), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("pending_github_apps", schema=None) as batch_op:
        batch_op.drop_column("setup_url")
