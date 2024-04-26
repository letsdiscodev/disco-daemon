"""0.5.0

Revision ID: 87c62632dfd1
Revises: 3eb8871ccb85
Create Date: 2024-04-25 20:45:49.295620

"""

import sqlalchemy as sa
from alembic import op

revision = "87c62632dfd1"
down_revision = "3eb8871ccb85"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "projects",
        sa.Column("github_webhook_secret", sa.String(length=32), nullable=True),
    )
    op.create_index(
        op.f("ix_projects_github_webhook_token"),
        "projects",
        ["github_webhook_token"],
        unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_projects_github_webhook_token"), table_name="projects")
    op.drop_column("projects", "github_webhook_secret")
