"""0.12.0

Revision ID: b570b8c2424d
Revises: 41a2f999a3e9
Create Date: 2024-06-05 01:12:09.118281

"""

import sqlalchemy as sa
from alembic import op

revision = "b570b8c2424d"
down_revision = "41a2f999a3e9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_index("ix_deployments_github_repo_id")
        batch_op.drop_constraint(
            "fk_deployments_github_repo_id_github_app_repos", type_="foreignkey"
        )
        batch_op.drop_column("github_repo_id")


def downgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("github_repo_id", sa.VARCHAR(length=32), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_deployments_github_repo_id_github_app_repos",
            "github_app_repos",
            ["github_repo_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_deployments_github_repo_id", ["github_repo_id"], unique=False
        )
