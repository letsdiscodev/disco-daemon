import logging

from fastapi import FastAPI

from disco.endpoints import (
    deployments,
    envvariables,
    logs,
    meta,
    projectkeyvalues,
    projects,
    run,
    syslog,
)
from disco.endpoints.webhooks import github

logging.basicConfig(level=logging.INFO)

log = logging.getLogger(__name__)

log.info("Initializing Disco daemon")
app = FastAPI()

app.include_router(meta.router)
app.include_router(projects.router)
app.include_router(deployments.router)
app.include_router(run.router)
app.include_router(envvariables.router)
app.include_router(projectkeyvalues.router)
app.include_router(logs.router)
app.include_router(syslog.router)
app.include_router(github.router)


@app.get("/")
def root_get():
    return {"disco": "/"}


log.info("Ready to disco")
