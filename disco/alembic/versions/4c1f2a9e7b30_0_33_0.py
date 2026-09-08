"""0.33.0

Revision ID: 4c1f2a9e7b30
Revises: d8adabff2804
Create Date: 2026-09-08 07:40:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "4c1f2a9e7b30"
down_revision = "d8adabff2804"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("deployment_type", sa.Unicode(length=255), nullable=True)
        )
    # existing deployments: a repository means GITHUB, unless the commit is the
    # one of the previous deployment and the env variables changed (a rebuild
    # after an env variable change; the same commit with the same variables is
    # a redeploy); without a repository, only disco files posted to the API
    # existed: FILES
    op.execute(
        "UPDATE deployments SET deployment_type = "
        "CASE WHEN github_repo_full_name IS NULL THEN 'FILES' ELSE 'GITHUB' END"
    )
    env_vars_of = (
        "SELECT name, value FROM deployment_env_variables WHERE deployment_id = {}"
    )
    op.execute(
        "UPDATE deployments SET deployment_type = 'ENV_VAR' "
        "WHERE github_repo_full_name IS NOT NULL AND commit_hash IS NOT NULL "
        "AND commit_hash = ("
        "SELECT prev.commit_hash FROM deployments AS prev "
        "WHERE prev.id = deployments.prev_deployment_id) "
        "AND (EXISTS ("
        + env_vars_of.format("deployments.id")
        + " EXCEPT "
        + env_vars_of.format("deployments.prev_deployment_id")
        + ") OR EXISTS ("
        + env_vars_of.format("deployments.prev_deployment_id")
        + " EXCEPT "
        + env_vars_of.format("deployments.id")
        + "))"
    )
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.alter_column("deployment_type", nullable=False)
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("deployment_type")


def downgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("deployment_type", sa.Unicode(length=255), nullable=True)
        )
    op.execute(
        "UPDATE projects SET deployment_type = 'GITHUB' WHERE id IN "
        "(SELECT project_id FROM project_github_repos)"
    )
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("deployment_type")
