import json
import logging
import time
from typing import Any

from disco.models.db import Session
from disco.utils.mq.handlers import HANDLERS
from disco.utils.mq.tasks import (
    get_next_task,
    get_task_by_id,
    mark_task_as_completed,
    mark_task_as_failed,
)

log = logging.getLogger(__name__)


class Consumer:
    def __init__(self):
        self.stopped = False

    def work(self):
        while not self.stopped:
            task_id, task_name, task_body = self._get_next_task()
            if task_id is None:
                time.sleep(0.5)
            else:
                try:
                    result = self._process_task(
                        task_id=task_id, task_name=task_name, task_body=task_body
                    )
                    self._mark_task_as_completed(task_id=task_id, result=result)
                except Exception:
                    log.exception(
                        "Exception processing task %s %s %s",
                        task_id,
                        task_name,
                        task_body,
                    )
                    self._mark_task_as_failed(
                        task_id=task_id, result=dict(reason="EXCEPTION")
                    )

    def stop(self):
        self.stopped = True

    def _get_next_task(
        self
    ) -> tuple[str, str, dict[str, Any]] | tuple[None, None, None]:
        with Session() as dbsession:
            with dbsession.begin():
                task = get_next_task(dbsession)
                if task is None:
                    return None, None, None
                return task.id, task.name, json.loads(task.body)

    def _mark_task_as_completed(self, task_id: str, result: dict[str, Any]):
        with Session() as dbsession:
            with dbsession.begin():
                task = get_task_by_id(dbsession, task_id)
                assert task is not None
                mark_task_as_completed(task, result)

    def _mark_task_as_failed(self, task_id: str, result: dict[str, Any]):
        with Session() as dbsession:
            with dbsession.begin():
                task = get_task_by_id(dbsession, task_id)
                assert task is not None
                mark_task_as_failed(task, result)

    def _process_task(
        self, task_id: str, task_name: str, task_body: dict[str, Any]
    ) -> None:
        HANDLERS[task_name](task_body=task_body)
