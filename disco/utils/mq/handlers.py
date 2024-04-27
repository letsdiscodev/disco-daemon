import logging

from disco.models.db import Session

log = logging.getLogger(__name__)


def process_github_webhook(task_body):
    import hashlib
    import hmac

    from disco.utils.deployments import create_deployment
    from disco.utils.github import get_commit_info_from_webhook_push
    from disco.utils.mq.tasks import enqueue_task_deprecated
    from disco.utils.projects import get_project_by_github_webhook_token

    webhook_token = task_body["webhook_token"]
    x_hub_signature_256 = task_body["x_hub_signature_256"]
    request_body_bytes = task_body["request_body_bytes"]
    request_body = request_body_bytes.decode("utf-8")
    log.info("Processing Github Webhook for project %s", webhook_token)
    log.info("Processing Github Webhook %s", request_body)
    branch, commit_hash = get_commit_info_from_webhook_push(request_body)
    if branch not in ["master", "main"]:
        log.info("Branch was not master or main, skipping")
        return

    with Session.begin() as dbsession:
        project = get_project_by_github_webhook_token(dbsession, webhook_token)
        if project is None:
            log.warning(
                "Project with Github Webhook Token not found, skipping %s",
                webhook_token,
            )
            return
        github_webhook_secret = project.github_webhook_secret
    if github_webhook_secret is not None:  # backward compat <= 0.4.1
        if x_hub_signature_256 is None:
            log.warning("X-Hub-Signature-256 not provided, skipping")
            return
        hash_object = hmac.new(
            github_webhook_secret.encode("utf-8"),
            msg=request_body_bytes,
            digestmod=hashlib.sha256,
        )
        expected_signature = "sha256=" + hash_object.hexdigest()
        if not hmac.compare_digest(expected_signature, x_hub_signature_256):
            log.warning("X-Hub-Signature-256 does not match, skipping")
            return
        log.info("Github webhook signature matched, continuing")
    else:
        log.info(
            "Legacy pre 0.4.1 project, no Github webhook signature to verify, continuing"
        )
    with Session.begin() as dbsession:
        project = get_project_by_github_webhook_token(dbsession, webhook_token)
        assert project is not None
        deployment = create_deployment(
            dbsession=dbsession,
            project=project,
            commit_hash=commit_hash,
            disco_file=None,
            by_api_key=None,
        )
        deployment_id = deployment.id
    enqueue_task_deprecated(
        task_name="PROCESS_DEPLOYMENT",
        body=dict(
            deployment_id=deployment_id,
        ),
    )


def process_deployment(task_body):
    from disco.utils.deploymentflow import process_deployment as process_deployment_func

    deployment_id = task_body["deployment_id"]
    process_deployment_func(deployment_id)


def process_deployment_if_any(task_body):
    from disco.utils.deployments import get_oldest_queued_deployment
    from disco.utils.mq.tasks import enqueue_task_deprecated
    from disco.utils.projects import get_project_by_id

    project_id = task_body["project_id"]
    with Session.begin() as dbsession:
        project = get_project_by_id(dbsession, project_id)
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
    PROCESS_GITHUB_WEBHOOK=process_github_webhook,
    PROCESS_DEPLOYMENT=process_deployment,
    PROCESS_DEPLOYMENT_IF_ANY=process_deployment_if_any,
)
