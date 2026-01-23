"""0.30.0

Revision ID: a1b2c3d4e5f6
Revises: b0b4edb3672a
Create Date: 2026-01-22 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "d8adabff2804"
down_revision = "b0b4edb3672a"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("docker_registry", sa.Unicode(length=2048), nullable=True)
        )

    op.execute("UPDATE deployments SET docker_registry = registry_host")

    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("registry_host")


def downgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("registry_host", sa.Unicode(length=2048), nullable=True)
        )

    op.execute("UPDATE deployments SET registry_host = docker_registry")

    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("docker_registry")
