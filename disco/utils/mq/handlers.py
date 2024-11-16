import logging

from disco.models.db import Session

log = logging.getLogger(__name__)


def process_deployment(task_body):
    from disco.utils.deploymentflow import process_deployment as process_deployment_func

    deployment_id = task_body["deployment_id"]
    process_deployment_func(deployment_id)


def process_deployment_if_any(task_body):
    from disco.utils.deployments import get_oldest_queued_deployment
    from disco.utils.mq.tasks import enqueue_task_deprecated
    from disco.utils.projects import get_project_by_id_sync

    project_id = task_body["project_id"]
    with Session.begin() as dbsession:
        project = get_project_by_id_sync(dbsession, project_id)
        if project is None:
            log.warning(
                "Project %s not found, not processing next deployment", project_id
            )
            return
        deployment = get_oldest_queued_deployment(dbsession, project)
        if deployment is None or deployment.status != "QUEUED":
            log.info(
                "No more queued deployments for project %s, done for now.",
                project.log(),
            )
            return
        enqueue_task_deprecated(
            task_name="PROCESS_DEPLOYMENT",
            body=dict(
                deployment_id=deployment.id,
            ),
        )


HANDLERS = dict(
    PROCESS_DEPLOYMENT=process_deployment,
    PROCESS_DEPLOYMENT_IF_ANY=process_deployment_if_any,
)
