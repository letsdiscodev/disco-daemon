"""0.8.0b

Revision ID: 7867432539d9
Revises: 3fe4af6efa33
Create Date: 2024-05-11 01:56:26.981025

"""

import sqlalchemy as sa
from alembic import op

revision = "7867432539d9"
down_revision = "3fe4af6efa33"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("project_github_repos", schema=None) as batch_op:
        batch_op.alter_column(
            "full_name", existing_type=sa.VARCHAR(length=255), nullable=False
        )
        batch_op.drop_index("ix_project_github_repos_github_app_repo_id")
        batch_op.drop_constraint(
            "fk_project_github_repos_github_app_repo_id_github_app_repos",
            type_="foreignkey",
        )
        batch_op.drop_column("github_app_repo_id")


def downgrade():
    with op.batch_alter_table("project_github_repos", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("github_app_repo_id", sa.VARCHAR(length=32), nullable=False)
        )
        batch_op.create_foreign_key(
            "fk_project_github_repos_github_app_repo_id_github_app_repos",
            "github_app_repos",
            ["github_app_repo_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_project_github_repos_github_app_repo_id",
            ["github_app_repo_id"],
            unique=False,
        )
        batch_op.alter_column(
            "full_name", existing_type=sa.VARCHAR(length=255), nullable=True
        )
