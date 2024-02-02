from fastapi import FastAPI

from disco.endpoints import deployments, envvariables, logs, projects, syslog
from disco.endpoints.webhooks import github

import logging

logging.basicConfig(level=logging.INFO)

log = logging.getLogger(__name__)

log.info("Initializing Disco daemon")
app = FastAPI()

app.include_router(projects.router)
app.include_router(deployments.router)
app.include_router(envvariables.router)
app.include_router(logs.router)
app.include_router(syslog.router)
app.include_router(github.router)


@app.get("/")
def read_main():
    return {"disco": True}

log.info("Ready to disco")