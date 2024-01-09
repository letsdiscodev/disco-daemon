import json
import logging
import uuid
from typing import Any

from sqlalchemy.orm.session import Session as DBSession

from disco.models import Task

log = logging.getLogger(__name__)


def enqueue_task(dbsession: DBSession, task_name: str, body: dict[str, Any]) -> None:
    task = Task(
        id=uuid.uuid4().hex,
        name=task_name,
        status="QUEUED",
        body=json.dumps(body),
    )
    log.info("Enqueued task %s", task_name)
    dbsession.add(task)


def get_task_by_id(dbsession: DBSession, task_id: str) -> Task | None:
    return dbsession.query(Task).get(task_id)


def get_next_task(dbsession: DBSession) -> Task | None:
    task = (
        dbsession.query(Task)
        .filter(Task.status == "QUEUED")
        .order_by(Task.created.asc())
        .first()
    )
    if task is not None:
        task.status = "PROCESSING"
    return task


def mark_task_as_completed(task: Task, result: dict[str, Any]) -> None:
    task.status = "COMPLETED"
    task.result = json.dumps(result)


def mark_task_as_failed(task: Task, result: dict[str, Any]) -> None:
    task.status = "FAILED"
    task.result = json.dumps(result)
