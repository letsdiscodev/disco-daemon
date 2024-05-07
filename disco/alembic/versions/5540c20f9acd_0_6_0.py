"""0.6.0

Revision ID: 89ebfedc4580
Revises: 87c62632dfd1
Create Date: 2024-05-07 19:24:47.609128

"""

import sqlalchemy as sa
from alembic import op

revision = "5540c20f9acd"
down_revision = "87c62632dfd1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "github_apps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.Column("slug", sa.Unicode(length=255), nullable=False),
        sa.Column("name", sa.Unicode(length=255), nullable=False),
        sa.Column("webhook_secret", sa.String(length=32), nullable=False),
        sa.Column("pem", sa.UnicodeText(), nullable=False),
        sa.Column("client_secret", sa.String(length=32), nullable=False),
        sa.Column("html_url", sa.Unicode(length=2000), nullable=False),
        sa.Column("app_info", sa.UnicodeText(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_apps")),
    )
    op.create_table(
        "pending_github_apps",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.Column("expires", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("organization", sa.Unicode(length=250), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_github_apps")),
    )
    op.create_table(
        "api_key_usages",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("api_key_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_api_key_usages_api_key_id_api_keys"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_key_usages")),
    )
    with op.batch_alter_table("api_key_usages", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_api_key_usages_api_key_id"), ["api_key_id"], unique=False
        )

    op.create_table(
        "github_app_installations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.Column("access_token", sa.UnicodeText(), nullable=True),
        sa.Column("access_token_expires", sa.DateTime(), nullable=True),
        sa.Column("github_app_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["github_app_id"],
            ["github_apps.id"],
            name=op.f("fk_github_app_installations_github_app_id_github_apps"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_app_installations")),
    )
    with op.batch_alter_table("github_app_installations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_github_app_installations_github_app_id"),
            ["github_app_id"],
            unique=False,
        )

    op.create_table(
        "github_app_repos",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.Column("installation_id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.Unicode(length=255), nullable=False),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["github_app_installations.id"],
            name=op.f("fk_github_app_repos_installation_id_github_app_installations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_app_repos")),
    )
    with op.batch_alter_table("github_app_repos", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_github_app_repos_full_name"), ["full_name"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_github_app_repos_installation_id"),
            ["installation_id"],
            unique=False,
        )

    op.create_table(
        "project_github_repos",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("github_app_repo_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["github_app_repo_id"],
            ["github_app_repos.id"],
            name=op.f("fk_project_github_repos_github_app_repo_id_github_app_repos"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_github_repos_project_id_projects"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_github_repos")),
    )
    with op.batch_alter_table("project_github_repos", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_project_github_repos_github_app_repo_id"),
            ["github_app_repo_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_project_github_repos_project_id"),
            ["project_id"],
            unique=False,
        )

    with op.batch_alter_table("api_key_invites", schema=None) as batch_op:
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=False)

    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=False)

    with op.batch_alter_table("command_outputs", schema=None) as batch_op:
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=False)

    with op.batch_alter_table("command_runs", schema=None) as batch_op:
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=False)

    with op.batch_alter_table("deployment_env_variables", schema=None) as batch_op:
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=False)

    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("github_repo_full_name", sa.Unicode(length=2048), nullable=True)
        )
        batch_op.add_column(
            sa.Column("github_repo_id", sa.String(length=32), nullable=True)
        )
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=False)
        batch_op.create_index(
            batch_op.f("ix_deployments_github_repo_id"),
            ["github_repo_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_deployments_github_repo_id_github_app_repos"),
            "github_app_repos",
            ["github_repo_id"],
            ["id"],
        )
        batch_op.drop_column("github_host")
        batch_op.drop_column("github_repo")

    with op.batch_alter_table("key_values", schema=None) as batch_op:
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=False)

    with op.batch_alter_table("project_env_variables", schema=None) as batch_op:
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=False)

    with op.batch_alter_table("project_key_values", schema=None) as batch_op:
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=False)

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("deployment_type", sa.Unicode(length=255), nullable=True)
        )
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=False)
        batch_op.drop_index("ix_projects_github_webhook_token")
        batch_op.drop_column("github_webhook_token")
        batch_op.drop_column("github_webhook_secret")
        batch_op.drop_column("github_repo")
        batch_op.drop_column("github_host")


def downgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("github_host", sa.VARCHAR(length=2048), nullable=True)
        )
        batch_op.add_column(
            sa.Column("github_repo", sa.VARCHAR(length=2048), nullable=True)
        )
        batch_op.add_column(
            sa.Column("github_webhook_secret", sa.VARCHAR(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("github_webhook_token", sa.VARCHAR(length=32), nullable=True)
        )
        batch_op.create_index(
            "ix_projects_github_webhook_token", ["github_webhook_token"], unique=False
        )
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=True)
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=True)
        batch_op.drop_column("deployment_type")

    with op.batch_alter_table("project_key_values", schema=None) as batch_op:
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=True)
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=True)

    with op.batch_alter_table("project_env_variables", schema=None) as batch_op:
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=True)
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=True)

    with op.batch_alter_table("key_values", schema=None) as batch_op:
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=True)
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=True)

    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("github_repo", sa.VARCHAR(length=2048), nullable=True)
        )
        batch_op.add_column(
            sa.Column("github_host", sa.VARCHAR(length=2048), nullable=True)
        )
        batch_op.drop_constraint(
            batch_op.f("fk_deployments_github_repo_id_github_app_repos"),
            type_="foreignkey",
        )
        batch_op.drop_index(batch_op.f("ix_deployments_github_repo_id"))
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=True)
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=True)
        batch_op.drop_column("github_repo_id")
        batch_op.drop_column("github_repo_full_name")

    with op.batch_alter_table("deployment_env_variables", schema=None) as batch_op:
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=True)
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=True)

    with op.batch_alter_table("command_runs", schema=None) as batch_op:
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=True)
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=True)

    with op.batch_alter_table("command_outputs", schema=None) as batch_op:
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=True)

    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=True)
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=True)

    with op.batch_alter_table("api_key_invites", schema=None) as batch_op:
        batch_op.alter_column("updated", existing_type=sa.DATETIME(), nullable=True)
        batch_op.alter_column("created", existing_type=sa.DATETIME(), nullable=True)

    with op.batch_alter_table("project_github_repos", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_project_github_repos_project_id"))
        batch_op.drop_index(batch_op.f("ix_project_github_repos_github_app_repo_id"))

    op.drop_table("project_github_repos")
    with op.batch_alter_table("github_app_repos", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_github_app_repos_installation_id"))
        batch_op.drop_index(batch_op.f("ix_github_app_repos_full_name"))

    op.drop_table("github_app_repos")
    with op.batch_alter_table("github_app_installations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_github_app_installations_github_app_id"))

    op.drop_table("github_app_installations")
    with op.batch_alter_table("api_key_usages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_key_usages_api_key_id"))

    op.drop_table("api_key_usages")
    op.drop_table("pending_github_apps")
    op.drop_table("github_apps")
