"""0.14.0

Revision ID: b2c4ac1469de
Revises: b570b8c2424d
Create Date: 2024-06-11 00:37:13.190145

"""

import sqlalchemy as sa
from alembic import op

revision = "b2c4ac1469de"
down_revision = "b570b8c2424d"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("branch", sa.Unicode(length=255), nullable=True))

    with op.batch_alter_table("project_github_repos", schema=None) as batch_op:
        batch_op.add_column(sa.Column("branch", sa.Unicode(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table("project_github_repos", schema=None) as batch_op:
        batch_op.drop_column("branch")

    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("branch")
