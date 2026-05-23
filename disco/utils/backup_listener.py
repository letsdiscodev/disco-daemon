"""Trigger a backup-and-push when an API key is created or deleted.

Subscribes to disco's internal event bus and reacts to apiKey:* events.
This is the "immediate freshness" path for the emergency-auth use case:
a newly-created admin key needs to be in every manager's local backup
file as soon as possible, in case the cluster locks up before the next
hourly cron runs.

Backup pushes can take ~5-30s; we don't want the API-key CRUD HTTP
response to block on that. So this listener runs as its own long-lived
asyncio task, draining the event queue and triggering backups out-of-
band. If a burst of API-key changes happens we coalesce — only one
push runs at a time.
"""

from __future__ import annotations

import asyncio
import logging

from disco.utils import events
from disco.utils.backup import make_and_push_periodic_backup

log = logging.getLogger(__name__)

_INTERESTING_EVENT_TYPES = {"apiKey:created", "apiKey:removed"}


async def watch_for_apikey_events_forever() -> None:
    """Top-level loop. Cancellable; tolerates exceptions and re-subscribes."""
    while True:
        try:
            await _watch_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("API-key backup listener crashed; restarting in 5s")
        await asyncio.sleep(5)


async def _watch_once() -> None:
    queue = events.subscribe()
    try:
        log.info("API-key backup listener subscribed")
        while True:
            event = await queue.get()
            if event.type not in _INTERESTING_EVENT_TYPES:
                continue
            # Coalesce: drain any further matching events queued behind this one,
            # then do a single push capturing the latest state.
            while not queue.empty():
                try:
                    next_event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_event.type not in _INTERESTING_EVENT_TYPES:
                    # Put it back at the head — except asyncio.Queue is FIFO with
                    # no put-front. We process it later; backup will just run twice
                    # in the unlikely worst case.
                    pass
            try:
                log.info("API key change observed; triggering backup-and-push")
                await make_and_push_periodic_backup()
            except Exception:
                log.exception("Backup-and-push after API-key event failed")
    finally:
        events.unsubscribe(queue)
