import asyncio
import logging
from typing import Any

from disco.utils.asyncworker import QueueTask, async_worker
from disco.utils.mq.handlers import HANDLERS

log = logging.getLogger(__name__)


def enqueue_task_deprecated(task_name: str, body: dict[str, Any]) -> None:
    log.info("Enqueuing task %s", task_name)

    def run_sync() -> None:
        HANDLERS[task_name](task_body=body)

    async def run_async() -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_sync)

    queue_task = QueueTask(run=run_async)

    async def enqueue():
        await async_worker.queue.put(queue_task)

    asyncio.run_coroutine_threadsafe(enqueue(), async_worker.get_loop())
