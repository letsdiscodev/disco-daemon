import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from disco.endpoints import (
    apikeyinvites,
    apikeys,
    cgi,
    corsorigins,
    deployments,
    dqlite_admin,
    envvariables,
    events,
    githubapps,
    internal_backups,
    logs,
    meta,
    nodes,
    projectdomains,
    projectkeyvalues,
    projects,
    registries,
    run,
    scale,
    syslog,
    tunnels,
    volumes,
)
from disco.middleware import middleware
from disco.models.db import AsyncReadSession
from disco.utils import caddy, keyvalues
from disco.utils.asyncworker import async_worker
from disco.utils.backup_distribution import cleanup_orphaned_push_jobs
from disco.utils.backup_listener import watch_for_apikey_events_forever
from disco.utils.deployments import (
    cleanup_deployments_on_disco_boot,
    enqueue_deployments_on_disco_boot,
)
from disco.utils.swarmwatcher import watch_swarm_events_forever

logging.basicConfig(level=logging.INFO)

log = logging.getLogger(__name__)

log.info("Initializing Disco daemon")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    async_worker.set_loop(loop)
    worker_task = loop.create_task(async_worker.work())
    swarm_watcher_task = loop.create_task(watch_swarm_events_forever())
    backup_listener_task = loop.create_task(watch_for_apikey_events_forever())
    # Seed the base Caddy config (disco_host route etc.) into the local Caddy if
    # absent; the config-sync module then publishes it to dqlite for all
    # instances. Idempotent and best-effort.
    async with AsyncReadSession() as dbsession:
        disco_host = await keyvalues.get_value(dbsession, "DISCO_HOST")
        tunnel_token = await keyvalues.get_value(dbsession, "CLOUDFLARE_TUNNEL_TOKEN")
    if disco_host is not None:
        await caddy.ensure_base_config(disco_host, tunnel=tunnel_token is not None)
    await cleanup_deployments_on_disco_boot()
    await enqueue_deployments_on_disco_boot()
    # Clean up any backup-push global-job services left over from a
    # previous daemon process (crashed mid-push, etc.). Must run before
    # any push trigger fires.
    await cleanup_orphaned_push_jobs()
    yield
    async_worker.stop()
    swarm_watcher_task.cancel()
    backup_listener_task.cancel()
    await worker_task
    for task in (swarm_watcher_task, backup_listener_task):
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan, middleware=middleware)

app.include_router(apikeyinvites.router)
app.include_router(apikeys.router)
app.include_router(cgi.router)
app.include_router(corsorigins.router)
app.include_router(deployments.router)
app.include_router(dqlite_admin.router)
app.include_router(envvariables.router)
app.include_router(events.router)
app.include_router(githubapps.router)
app.include_router(internal_backups.router)
app.include_router(logs.router)
app.include_router(meta.router)
app.include_router(nodes.router)
app.include_router(projectdomains.router)
app.include_router(projectkeyvalues.router)
app.include_router(projects.router)
app.include_router(registries.router)
app.include_router(run.router)
app.include_router(scale.router)
app.include_router(syslog.router)
app.include_router(tunnels.router)
app.include_router(volumes.router)


@app.get("/")
def root_get():
    return {"disco": True}


log.info("Ready to disco")
