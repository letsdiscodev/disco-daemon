"""0.8.0a

Revision ID: 3fe4af6efa33
Revises: 47da35039f6f
Create Date: 2024-05-11 01:44:07.784733

"""

import sqlalchemy as sa
from alembic import op

revision = "3fe4af6efa33"
down_revision = "47da35039f6f"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("project_github_repos", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("full_name", sa.Unicode(length=255), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_project_github_repos_full_name"), ["full_name"], unique=False
        )


def downgrade():
    with op.batch_alter_table("project_github_repos", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_project_github_repos_full_name"))
        batch_op.drop_column("full_name")
