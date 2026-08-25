from __future__ import annotations

import asyncio
import logging

from disco.utils import events
from disco.utils.backup import make_and_push_periodic_backup

log = logging.getLogger(__name__)

_INTERESTING_EVENT_TYPES = {"apiKey:created", "apiKey:removed"}


async def watch_for_apikey_events_forever() -> None:
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
            # Drain the burst so it produces a single push.
            while not queue.empty():
                try:
                    next_event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_event.type not in _INTERESTING_EVENT_TYPES:
                    # Dropped; asyncio.Queue has no put-front.
                    pass
            try:
                log.info("API key change observed; triggering backup-and-push")
                await make_and_push_periodic_backup()
            except Exception:
                log.exception("Backup-and-push after API-key event failed")
    finally:
        events.unsubscribe(queue)
