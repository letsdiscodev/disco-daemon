"""0.2.0

Revision ID: eba27af20db2
Revises: b09bcf2ef8df
Create Date: 2024-03-23 20:25:40.386154

"""
import sqlalchemy as sa
from alembic import op

revision = "eba27af20db2"
down_revision = "b09bcf2ef8df"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "deployments",
        sa.Column("registry_host", sa.Unicode(length=2048), nullable=True),
    )


def downgrade():
    op.drop_column("deployments", "registry_host")
