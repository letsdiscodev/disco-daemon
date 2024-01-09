import json
import logging
import time
from typing import Any

import transaction
from sqlalchemy.orm import sessionmaker

from disco.models import get_tm_session
from disco.utils.mq.handlers import HANDLERS
from disco.utils.mq.tasks import (
    get_next_task,
    get_task_by_id,
    mark_task_as_completed,
    mark_task_as_failed,
)

log = logging.getLogger(__name__)


class Consumer:
    def __init__(self, session_factory: sessionmaker):
        self.session_factory = session_factory
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
        def inner(dbsession):
            task = get_next_task(dbsession)
            if task is None:
                return None, None, None
            return task.id, task.name, json.loads(task.body)

        return self._with_dbsession(inner)

    def _mark_task_as_completed(self, task_id: str, result: dict[str, Any]):
        def inner(dbsession):
            task = get_task_by_id(dbsession, task_id)
            mark_task_as_completed(task, result)

        self._with_dbsession(inner)

    def _mark_task_as_failed(self, task_id: str, result: dict[str, Any]):
        def inner(dbsession):
            task = get_task_by_id(dbsession, task_id)
            mark_task_as_failed(task, result)

        self._with_dbsession(inner)

    def _with_dbsession(self, func):
        for attempt in transaction.manager.attempts(6):
            with attempt:
                dbsession = get_tm_session(self.session_factory, transaction.manager)
                return func(dbsession)

    def _process_task(
        self, task_id: str, task_name: str, task_body: dict[str, Any]
    ) -> None:
        HANDLERS[task_name](with_dbsession=self._with_dbsession, task_body=task_body)
