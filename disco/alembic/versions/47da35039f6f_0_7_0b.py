"""0.7.0b

Revision ID: 47da35039f6f
Revises: 97e98737cba8
Create Date: 2024-05-07 23:59:06.826118

"""

import sqlalchemy as sa
from alembic import op

revision = "47da35039f6f"
down_revision = "97e98737cba8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("domain")

    with op.batch_alter_table("github_apps", schema=None) as batch_op:
        batch_op.alter_column("owner_id", existing_type=sa.INTEGER(), nullable=False)
        batch_op.alter_column(
            "owner_login", existing_type=sa.VARCHAR(length=255), nullable=False
        )
        batch_op.alter_column(
            "owner_type", existing_type=sa.VARCHAR(length=255), nullable=False
        )

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("domain")


def downgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("domain", sa.VARCHAR(length=255), nullable=True))

    with op.batch_alter_table("github_apps", schema=None) as batch_op:
        batch_op.alter_column(
            "owner_type", existing_type=sa.VARCHAR(length=255), nullable=True
        )
        batch_op.alter_column(
            "owner_login", existing_type=sa.VARCHAR(length=255), nullable=True
        )
        batch_op.alter_column("owner_id", existing_type=sa.INTEGER(), nullable=True)

    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("domain", sa.VARCHAR(length=255), nullable=True))
