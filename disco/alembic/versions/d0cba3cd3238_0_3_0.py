"""0.3.0

Revision ID: d0cba3cd3238
Revises: eba27af20db2
Create Date: 2024-04-03 01:34:39.255972

"""
import sqlalchemy as sa
from alembic import op

revision = "d0cba3cd3238"
down_revision = "eba27af20db2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "api_key_invites",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.Column("name", sa.Unicode(length=255), nullable=False),
        sa.Column("expires", sa.DateTime(), nullable=False),
        sa.Column("by_api_key_id", sa.String(length=32), nullable=False),
        sa.Column("api_key_id", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_api_key_invites_api_key_id_api_keys"),
        ),
        sa.ForeignKeyConstraint(
            ["by_api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_api_key_invites_by_api_key_id_api_keys"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_key_invites")),
    )
    op.create_index(
        op.f("ix_api_key_invites_api_key_id"),
        "api_key_invites",
        ["api_key_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_api_key_invites_by_api_key_id"),
        "api_key_invites",
        ["by_api_key_id"],
        unique=False,
    )
    op.execute("ALTER TABLE api_keys RENAME COLUMN log_id TO public_key;")
    op.add_column("api_keys", sa.Column("deleted", sa.DateTime(), nullable=True))
    op.create_index(
        op.f("ix_api_keys_public_key"), "api_keys", ["public_key"], unique=False
    )


def downgrade():
    op.drop_index(op.f("ix_api_keys_public_key"), table_name="api_keys")
    op.drop_column("api_keys", "deleted")
    op.execute("ALTER TABLE api_keys RENAME COLUMN public_key TO log_id;")
    op.drop_index(
        op.f("ix_api_key_invites_by_api_key_id"), table_name="api_key_invites"
    )
    op.drop_index(op.f("ix_api_key_invites_api_key_id"), table_name="api_key_invites")
    op.drop_table("api_key_invites")
