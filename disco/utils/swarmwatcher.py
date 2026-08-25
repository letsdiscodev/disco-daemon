from __future__ import annotations

import asyncio
import logging
from asyncio import subprocess

from disco.utils.swarmreconciler import reconcile_dqlite_services

log = logging.getLogger(__name__)


async def watch_swarm_events_forever() -> None:
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
    # An unread stderr pipe would fill up and block `docker events`.
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
        # Catch up on events missed while the stream was down.
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
