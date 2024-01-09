import logging

from cornice import Service
from pyramid.httpexceptions import HTTPOk

from disco.http.contexts.projects import SingleByIdContext
from disco.utils.mq.tasks import enqueue_task

log = logging.getLogger(__name__)

github_webhook_service = Service(
    name="github_webhook_service",
    path="/webhooks/github/{project_id}",
    http_cache=(None, dict(private=True)),
    factory=SingleByIdContext,
)


@github_webhook_service.post()
def github_webhook_service_post(request):
    event_type = request.headers.get("X-GitHub-Event")
    if event_type != "push":
        log.info(
            "Ignoring Github webhook (not a push) %s for %s",
            event_type,
            request.matchdict["project_id"],
        )
        return HTTPOk()
    log.info(
        "Received Github webhook %s for %s", event_type, request.matchdict["project_id"]
    )
    enqueue_task(
        dbsession=request.dbsession,
        task_name="PROCESS_GITHUB_WEBHOOK",
        body=dict(
            project_id=request.matchdict["project_id"],
            request_body=request.text,
        ),
    )
    return HTTPOk()
