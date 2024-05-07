"""0.7.0 Part A

Revision ID: 97e98737cba8
Revises: 5540c20f9acd
Create Date: 2024-05-07 19:28:07.696067

"""

import sqlalchemy as sa
from alembic import op

revision = "97e98737cba8"
down_revision = "5540c20f9acd"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_domains",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.Column("name", sa.Unicode(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_domains_project_id_projects"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_domains")),
    )
    with op.batch_alter_table("project_domains", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_project_domains_name"), ["name"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_project_domains_project_id"), ["project_id"], unique=False
        )

    with op.batch_alter_table("github_apps", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("owner_login", sa.Unicode(length=255), nullable=True)
        )
        batch_op.add_column(
            sa.Column("owner_type", sa.Unicode(length=255), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("github_apps", schema=None) as batch_op:
        batch_op.drop_column("owner_type")
        batch_op.drop_column("owner_login")
        batch_op.drop_column("owner_id")

    with op.batch_alter_table("project_domains", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_project_domains_project_id"))
        batch_op.drop_index(batch_op.f("ix_project_domains_name"))

    op.drop_table("project_domains")
