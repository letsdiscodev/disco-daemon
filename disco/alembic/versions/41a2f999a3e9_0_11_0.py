"""0.11.0

Revision ID: 41a2f999a3e9
Revises: 7867432539d9
Create Date: 2024-05-18 00:49:24.293133

"""

import sqlalchemy as sa
from alembic import op

revision = "41a2f999a3e9"
down_revision = "7867432539d9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("command_outputs", schema=None) as batch_op:
        batch_op.drop_index("ix_command_outputs_created")
        batch_op.drop_index("ix_command_outputs_source")

    op.drop_table("command_outputs")


def downgrade():
    op.create_table(
        "command_outputs",
        sa.Column("id", sa.VARCHAR(length=32), nullable=False),
        sa.Column("created", sa.DATETIME(), nullable=False),
        sa.Column("source", sa.VARCHAR(length=100), nullable=False),
        sa.Column("text", sa.TEXT(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_command_outputs"),
    )
    with op.batch_alter_table("command_outputs", schema=None) as batch_op:
        batch_op.create_index("ix_command_outputs_source", ["source"], unique=False)
        batch_op.create_index("ix_command_outputs_created", ["created"], unique=False)
