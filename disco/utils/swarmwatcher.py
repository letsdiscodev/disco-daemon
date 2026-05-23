"""Stream Docker Swarm node-lifecycle events and trigger the reconciler.

Swarm emits events on every manager when a node joins, leaves, changes
role, transitions between ready/down/disconnected, etc. We don't parse
the event payload — any node event is a hint that the reconciler should
look again. The reconciler itself is the source of truth, this is just
a "wake up sooner" signal layered on top of the periodic 1-minute pass.

If the stream dies (daemon restart, network blip), the loop restarts it
after a small backoff. Missed events during a disconnect are caught up
by the next periodic reconcile.
"""

from __future__ import annotations

import asyncio
import logging
from asyncio import subprocess

from disco.utils.swarmreconciler import reconcile_dqlite_services

log = logging.getLogger(__name__)


async def watch_swarm_events_forever() -> None:
    """Top-level loop. Never returns under normal operation."""
    while True:
        try:
            await _watch_one_stream()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("docker events watcher crashed; restarting in 5s")
        await asyncio.sleep(5)


async def _watch_one_stream() -> None:
    log.info("Starting docker events watcher (type=node)")
    # Discard stderr — we never read it, and leaving stderr=PIPE while not
    # consuming it would let `docker events` block on a full pipe and
    # silently stop emitting events.
    process = await asyncio.create_subprocess_exec(
        "docker",
        "events",
        "--filter",
        "type=node",
        "--format",
        "{{.Type}} {{.Action}} {{.Actor.Attributes}}",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    try:
        # Trigger an initial reconcile in case we missed events while the
        # daemon was down or this stream was reconnecting.
        await reconcile_dqlite_services()
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            log.info("swarm event: %s", line)
            await reconcile_dqlite_services()
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
