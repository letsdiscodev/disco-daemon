import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from disco.endpoints import (
    apikeyinvites,
    apikeys,
    deployments,
    envvariables,
    logs,
    meta,
    nodes,
    projectkeyvalues,
    projects,
    run,
    scale,
    syslog,
    volumes,
)
from disco.endpoints.webhooks import github
from disco.utils.asyncworker import async_worker

logging.basicConfig(level=logging.INFO)

log = logging.getLogger(__name__)

log.info("Initializing Disco daemon")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    async_worker.set_loop(loop)
    worker_task = loop.create_task(async_worker.work())
    yield
    async_worker.stop()
    await worker_task


app = FastAPI(lifespan=lifespan)

app.include_router(meta.router)
app.include_router(projects.router)
app.include_router(volumes.router)
app.include_router(deployments.router)
app.include_router(run.router)
app.include_router(envvariables.router)
app.include_router(projectkeyvalues.router)
app.include_router(logs.router)
app.include_router(nodes.router)
app.include_router(scale.router)
app.include_router(apikeys.router)
app.include_router(apikeyinvites.router)
app.include_router(syslog.router)
app.include_router(github.router)


@app.get("/")
def root_get():
    return {"disco": True}


log.info("Ready to disco")
