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
    envvariables,
    events,
    githubapps,
    logs,
    meta,
    nodes,
    projectdomains,
    projectkeyvalues,
    projects,
    run,
    scale,
    shell,
    syslog,
    tunnels,
    volumes,
)
from disco.middleware import middleware
from disco.utils.asyncworker import async_worker
from disco.utils.deployments import (
    cleanup_deployments_on_disco_boot,
    enqueue_deployments_on_disco_boot,
)

logging.basicConfig(level=logging.INFO)

log = logging.getLogger(__name__)

log.info("Initializing Disco daemon")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    async_worker.set_loop(loop)
    worker_task = loop.create_task(async_worker.work())
    await cleanup_deployments_on_disco_boot()
    await enqueue_deployments_on_disco_boot()
    yield
    async_worker.stop()
    await worker_task


app = FastAPI(lifespan=lifespan, middleware=middleware)

app.include_router(meta.router)
app.include_router(projects.router)
app.include_router(volumes.router)
app.include_router(deployments.router)
app.include_router(run.router)
app.include_router(shell.router)
app.include_router(envvariables.router)
app.include_router(projectdomains.router)
app.include_router(projectkeyvalues.router)
app.include_router(logs.router)
app.include_router(nodes.router)
app.include_router(scale.router)
app.include_router(apikeys.router)
app.include_router(apikeyinvites.router)
app.include_router(syslog.router)
app.include_router(tunnels.router)
app.include_router(corsorigins.router)
app.include_router(cgi.router)
app.include_router(githubapps.router)
app.include_router(events.router)


@app.get("/")
def root_get():
    return {"disco": True}


log.info("Ready to disco")
