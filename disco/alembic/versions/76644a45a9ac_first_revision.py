"""First revision

Revision ID: 76644a45a9ac
Revises:
Create Date: 2024-02-21 00:19:57.894491

"""
import sqlalchemy as sa
from alembic import op

revision = "76644a45a9ac"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.Column("name", sa.Unicode(length=255), nullable=False),
        sa.Column("log_id", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
    )
    op.create_table(
        "command_outputs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("text", sa.UnicodeText(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_command_outputs")),
    )
    op.create_index(
        op.f("ix_command_outputs_created"), "command_outputs", ["created"], unique=False
    )
    op.create_index(
        op.f("ix_command_outputs_source"), "command_outputs", ["source"], unique=False
    )
    op.create_table(
        "key_values",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.Column("value", sa.UnicodeText(), nullable=True),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_key_values")),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.Column("name", sa.Unicode(length=255), nullable=False),
        sa.Column("domain", sa.Unicode(length=255), nullable=True),
        sa.Column("github_repo", sa.Unicode(length=2048), nullable=True),
        sa.Column("github_webhook_token", sa.String(length=32), nullable=True),
        sa.Column("github_host", sa.Unicode(length=2048), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Unicode(length=10000), nullable=False),
        sa.Column("result", sa.Unicode(length=10000), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    op.create_table(
        "deployments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("commit_hash", sa.String(length=200), nullable=True),
        sa.Column("disco_file", sa.Unicode(length=5000), nullable=True),
        sa.Column("project_name", sa.Unicode(length=255), nullable=False),
        sa.Column("github_repo", sa.Unicode(length=2048), nullable=True),
        sa.Column("github_host", sa.Unicode(length=2048), nullable=True),
        sa.Column("domain", sa.Unicode(length=255), nullable=True),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("prev_deployment_id", sa.String(length=32), nullable=True),
        sa.Column("by_api_key_id", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["by_api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_deployments_by_api_key_id_api_keys"),
        ),
        sa.ForeignKeyConstraint(
            ["prev_deployment_id"],
            ["deployments.id"],
            name=op.f("fk_deployments_prev_deployment_id_deployments"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_deployments_project_id_projects"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
    )
    op.create_index(
        op.f("ix_deployments_by_api_key_id"),
        "deployments",
        ["by_api_key_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deployments_number"), "deployments", ["number"], unique=False
    )
    op.create_index(
        op.f("ix_deployments_prev_deployment_id"),
        "deployments",
        ["prev_deployment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deployments_project_id"), "deployments", ["project_id"], unique=False
    )
    op.create_table(
        "project_env_variables",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Unicode(length=4000), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("by_api_key_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["by_api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_project_env_variables_by_api_key_id_api_keys"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_env_variables_project_id_projects"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_env_variables")),
    )
    op.create_index(
        op.f("ix_project_env_variables_by_api_key_id"),
        "project_env_variables",
        ["by_api_key_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_env_variables_name"),
        "project_env_variables",
        ["name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_env_variables_project_id"),
        "project_env_variables",
        ["project_id"],
        unique=False,
    )
    op.create_table(
        "command_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("service", sa.Unicode(), nullable=False),
        sa.Column("command", sa.UnicodeText(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("deployment_id", sa.String(length=32), nullable=True),
        sa.Column("by_api_key_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["by_api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_command_runs_by_api_key_id_api_keys"),
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name=op.f("fk_command_runs_deployment_id_deployments"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_command_runs_project_id_projects"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_command_runs")),
    )
    op.create_index(
        op.f("ix_command_runs_by_api_key_id"),
        "command_runs",
        ["by_api_key_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_command_runs_deployment_id"),
        "command_runs",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_command_runs_number"), "command_runs", ["number"], unique=False
    )
    op.create_index(
        op.f("ix_command_runs_project_id"), "command_runs", ["project_id"], unique=False
    )
    op.create_table(
        "deployment_env_variables",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Unicode(length=4000), nullable=False),
        sa.Column("deployment_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name=op.f("fk_deployment_env_variables_deployment_id_deployments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_env_variables")),
    )
    op.create_index(
        op.f("ix_deployment_env_variables_deployment_id"),
        "deployment_env_variables",
        ["deployment_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_deployment_env_variables_deployment_id"),
        table_name="deployment_env_variables",
    )
    op.drop_table("deployment_env_variables")
    op.drop_index(op.f("ix_command_runs_project_id"), table_name="command_runs")
    op.drop_index(op.f("ix_command_runs_number"), table_name="command_runs")
    op.drop_index(op.f("ix_command_runs_deployment_id"), table_name="command_runs")
    op.drop_index(op.f("ix_command_runs_by_api_key_id"), table_name="command_runs")
    op.drop_table("command_runs")
    op.drop_index(
        op.f("ix_project_env_variables_project_id"), table_name="project_env_variables"
    )
    op.drop_index(
        op.f("ix_project_env_variables_name"), table_name="project_env_variables"
    )
    op.drop_index(
        op.f("ix_project_env_variables_by_api_key_id"),
        table_name="project_env_variables",
    )
    op.drop_table("project_env_variables")
    op.drop_index(op.f("ix_deployments_project_id"), table_name="deployments")
    op.drop_index(op.f("ix_deployments_prev_deployment_id"), table_name="deployments")
    op.drop_index(op.f("ix_deployments_number"), table_name="deployments")
    op.drop_index(op.f("ix_deployments_by_api_key_id"), table_name="deployments")
    op.drop_table("deployments")
    op.drop_table("tasks")
    op.drop_table("projects")
    op.drop_table("key_values")
    op.drop_index(op.f("ix_command_outputs_source"), table_name="command_outputs")
    op.drop_index(op.f("ix_command_outputs_created"), table_name="command_outputs")
    op.drop_table("command_outputs")
    op.drop_table("api_keys")
