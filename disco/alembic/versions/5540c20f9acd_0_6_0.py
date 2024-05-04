"""0.6.0

Revision ID: 5540c20f9acd
Revises: 87c62632dfd1
Create Date: 2024-05-04 22:54:34.928878

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
    op.create_index(
        op.f("ix_api_key_usages_api_key_id"),
        "api_key_usages",
        ["api_key_id"],
        unique=False,
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
    op.create_index(
        op.f("ix_github_app_installations_github_app_id"),
        "github_app_installations",
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
    op.create_index(
        op.f("ix_github_app_repos_full_name"),
        "github_app_repos",
        ["full_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_github_app_repos_installation_id"),
        "github_app_repos",
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
    op.create_index(
        op.f("ix_project_github_repos_github_app_repo_id"),
        "project_github_repos",
        ["github_app_repo_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_github_repos_project_id"),
        "project_github_repos",
        ["project_id"],
        unique=False,
    )
    op.alter_column(
        "api_key_invites", "created", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column(
        "api_key_invites", "updated", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column("api_keys", "created", existing_type=sa.DATETIME(), nullable=False)
    op.alter_column("api_keys", "updated", existing_type=sa.DATETIME(), nullable=False)
    op.alter_column(
        "command_outputs", "created", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column(
        "command_runs", "created", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column(
        "command_runs", "updated", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column(
        "deployment_env_variables",
        "created",
        existing_type=sa.DATETIME(),
        nullable=False,
    )
    op.alter_column(
        "deployment_env_variables",
        "updated",
        existing_type=sa.DATETIME(),
        nullable=False,
    )
    op.add_column(
        "deployments",
        sa.Column("github_repo_full_name", sa.Unicode(length=2048), nullable=True),
    )
    op.add_column(
        "deployments", sa.Column("github_repo_id", sa.String(length=32), nullable=True)
    )
    op.alter_column(
        "deployments", "created", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column(
        "deployments", "updated", existing_type=sa.DATETIME(), nullable=False
    )
    op.create_index(
        op.f("ix_deployments_github_repo_id"),
        "deployments",
        ["github_repo_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_deployments_github_repo_id_github_app_repos"),
        "deployments",
        "github_app_repos",
        ["github_repo_id"],
        ["id"],
    )
    op.drop_column("deployments", "github_host")
    op.drop_column("deployments", "github_repo")
    op.alter_column(
        "key_values", "created", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column(
        "key_values", "updated", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column(
        "project_env_variables", "created", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column(
        "project_env_variables", "updated", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column(
        "project_key_values", "created", existing_type=sa.DATETIME(), nullable=False
    )
    op.alter_column(
        "project_key_values", "updated", existing_type=sa.DATETIME(), nullable=False
    )
    op.add_column(
        "projects", sa.Column("deployment_type", sa.Unicode(length=255), nullable=True)
    )
    op.alter_column("projects", "created", existing_type=sa.DATETIME(), nullable=False)
    op.alter_column("projects", "updated", existing_type=sa.DATETIME(), nullable=False)
    op.drop_index("ix_projects_github_webhook_token", table_name="projects")
    op.drop_column("projects", "github_webhook_secret")
    op.drop_column("projects", "github_host")
    op.drop_column("projects", "github_repo")
    op.drop_column("projects", "github_webhook_token")


def downgrade():
    op.add_column(
        "projects",
        sa.Column("github_webhook_token", sa.VARCHAR(length=32), nullable=True),
    )
    op.add_column(
        "projects", sa.Column("github_repo", sa.VARCHAR(length=2048), nullable=True)
    )
    op.add_column(
        "projects", sa.Column("github_host", sa.VARCHAR(length=2048), nullable=True)
    )
    op.add_column(
        "projects",
        sa.Column("github_webhook_secret", sa.VARCHAR(length=32), nullable=True),
    )
    op.create_index(
        "ix_projects_github_webhook_token",
        "projects",
        ["github_webhook_token"],
        unique=False,
    )
    op.alter_column("projects", "updated", existing_type=sa.DATETIME(), nullable=True)
    op.alter_column("projects", "created", existing_type=sa.DATETIME(), nullable=True)
    op.drop_column("projects", "deployment_type")
    op.alter_column(
        "project_key_values", "updated", existing_type=sa.DATETIME(), nullable=True
    )
    op.alter_column(
        "project_key_values", "created", existing_type=sa.DATETIME(), nullable=True
    )
    op.alter_column(
        "project_env_variables", "updated", existing_type=sa.DATETIME(), nullable=True
    )
    op.alter_column(
        "project_env_variables", "created", existing_type=sa.DATETIME(), nullable=True
    )
    op.alter_column("key_values", "updated", existing_type=sa.DATETIME(), nullable=True)
    op.alter_column("key_values", "created", existing_type=sa.DATETIME(), nullable=True)
    op.add_column(
        "deployments", sa.Column("github_repo", sa.VARCHAR(length=2048), nullable=True)
    )
    op.add_column(
        "deployments", sa.Column("github_host", sa.VARCHAR(length=2048), nullable=True)
    )
    op.drop_constraint(
        op.f("fk_deployments_github_repo_id_github_app_repos"),
        "deployments",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_deployments_github_repo_id"), table_name="deployments")
    op.alter_column(
        "deployments", "updated", existing_type=sa.DATETIME(), nullable=True
    )
    op.alter_column(
        "deployments", "created", existing_type=sa.DATETIME(), nullable=True
    )
    op.drop_column("deployments", "github_repo_id")
    op.drop_column("deployments", "github_repo_full_name")
    op.alter_column(
        "deployment_env_variables",
        "updated",
        existing_type=sa.DATETIME(),
        nullable=True,
    )
    op.alter_column(
        "deployment_env_variables",
        "created",
        existing_type=sa.DATETIME(),
        nullable=True,
    )
    op.alter_column(
        "command_runs", "updated", existing_type=sa.DATETIME(), nullable=True
    )
    op.alter_column(
        "command_runs", "created", existing_type=sa.DATETIME(), nullable=True
    )
    op.alter_column(
        "command_outputs", "created", existing_type=sa.DATETIME(), nullable=True
    )
    op.alter_column("api_keys", "updated", existing_type=sa.DATETIME(), nullable=True)
    op.alter_column("api_keys", "created", existing_type=sa.DATETIME(), nullable=True)
    op.alter_column(
        "api_key_invites", "updated", existing_type=sa.DATETIME(), nullable=True
    )
    op.alter_column(
        "api_key_invites", "created", existing_type=sa.DATETIME(), nullable=True
    )
    op.drop_index(
        op.f("ix_project_github_repos_project_id"), table_name="project_github_repos"
    )
    op.drop_index(
        op.f("ix_project_github_repos_github_app_repo_id"),
        table_name="project_github_repos",
    )
    op.drop_table("project_github_repos")
    op.drop_index(
        op.f("ix_github_app_repos_installation_id"), table_name="github_app_repos"
    )
    op.drop_index(op.f("ix_github_app_repos_full_name"), table_name="github_app_repos")
    op.drop_table("github_app_repos")
    op.drop_index(
        op.f("ix_github_app_installations_github_app_id"),
        table_name="github_app_installations",
    )
    op.drop_table("github_app_installations")
    op.drop_index(op.f("ix_api_key_usages_api_key_id"), table_name="api_key_usages")
    op.drop_table("api_key_usages")
    op.drop_table("pending_github_apps")
    op.drop_table("github_apps")
