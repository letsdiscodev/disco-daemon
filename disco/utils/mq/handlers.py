import logging

from disco.models.db import Session

log = logging.getLogger(__name__)


def process_github_webhook(task_body):
    from disco.utils.deployments import create_deployment
    from disco.utils.github import get_commit_info_from_webhook_push
    from disco.utils.projects import get_project_by_github_webhook_token

    webhook_token = task_body["webhook_token"]
    request_body = task_body["request_body"]
    log.info("Processing Github Webhook for project %s", webhook_token)
    log.info("Processing Github Webhook %s", request_body)
    branch, commit_hash = get_commit_info_from_webhook_push(request_body)
    if branch not in ["master", "main"]:
        log.info("Branch was not master or main, skipping")
        return

    with Session() as dbsession:
        with dbsession.begin():
            project = get_project_by_github_webhook_token(dbsession, webhook_token)
            if project is None:
                log.warning(
                    "Project with Github Webhook Token not found, skipping %s",
                    webhook_token,
                )
                return
            create_deployment(
                dbsession=dbsession,
                project=project,
                commit_hash=commit_hash,
                disco_file=None,
                by_api_key=None,
            )


def process_deployment(task_body):
    from disco.utils.deploymentflow import process_deployment as process_deployment_func

    deployment_id = task_body["deployment_id"]
    process_deployment_func(deployment_id)


def set_syslog_service(task_body):
    from disco.utils import docker, keyvalues, syslog

    db_data = dict()

    with Session() as dbsession:
        with dbsession.begin():
            db_data["disco_host"] = keyvalues.get_value(dbsession, "DISCO_HOST")
            db_data["urls"] = syslog.get_syslog_urls(dbsession)

    docker.set_syslog_service(db_data["disco_host"], db_data["urls"])


HANDLERS = dict(
    PROCESS_GITHUB_WEBHOOK=process_github_webhook,
    PROCESS_DEPLOYMENT=process_deployment,
    SET_SYSLOG_SERVICE=set_syslog_service,
)
