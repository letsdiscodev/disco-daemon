"""0.17.0

Revision ID: 26877eda6774
Revises: b2c4ac1469de
Create Date: 2024-08-21 00:26:43.456565

"""

import sqlalchemy as sa
from alembic import op

revision = "26877eda6774"
down_revision = "b2c4ac1469de"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cors_origins",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.Column("origin", sa.Unicode(length=255), nullable=False),
        sa.Column("by_api_key_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["by_api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_cors_origins_by_api_key_id_api_keys"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cors_origins")),
    )
    with op.batch_alter_table("cors_origins", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_cors_origins_by_api_key_id"), ["by_api_key_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_cors_origins_origin"), ["origin"], unique=True
        )


def downgrade():
    with op.batch_alter_table("cors_origins", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cors_origins_origin"))
        batch_op.drop_index(batch_op.f("ix_cors_origins_by_api_key_id"))

    op.drop_table("cors_origins")
