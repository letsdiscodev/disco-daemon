import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm.session import Session as DBSession

from disco.models.db import get_db
from disco.utils.mq.tasks import enqueue_task

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhooks/github/{project_id}", status_code=202)
def github_webhook_service_post(
    project_id: str,
    request: Request,
    dbsession: Annotated[DBSession, Depends(get_db)],
    x_github_event: Annotated[str | None, Header()],
):
    if x_github_event != "push":
        log.info(
            "Ignoring Github webhook (not a push) %s for %s",
            x_github_event,
            project_id,
        )
        return {}
    log.info("Received Github webhook %s for %s", x_github_event, project_id)
    enqueue_task(
        dbsession=dbsession,
        task_name="PROCESS_GITHUB_WEBHOOK",
        body=dict(
            project_id=project_id,
            request_body=asyncio.run(request.body()).decode("utf-8"),
        ),
    )
    return {}