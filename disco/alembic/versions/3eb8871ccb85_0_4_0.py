"""0.4.0

Revision ID: 3eb8871ccb85
Revises: d0cba3cd3238
Create Date: 2024-04-11 01:24:05.745167

"""

import sqlalchemy as sa
from alembic import op

revision = "3eb8871ccb85"
down_revision = "d0cba3cd3238"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("tasks")


def downgrade():
    op.create_table(
        "tasks",
        sa.Column("id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("created", sa.DATETIME(), nullable=True),
        sa.Column("updated", sa.DATETIME(), nullable=True),
        sa.Column("name", sa.VARCHAR(length=255), nullable=False),
        sa.Column("status", sa.VARCHAR(length=32), nullable=False),
        sa.Column("body", sa.VARCHAR(length=10000), nullable=False),
        sa.Column("result", sa.VARCHAR(length=10000), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
    )
