import asyncio
import logging
import random
from secrets import token_hex

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError

from disco.auth import get_api_key_wo_tx
from disco.models.db import AsyncSession
from disco.utils import docker
from disco.utils.deployments import get_live_deployment
from disco.utils.discofile import get_disco_file_from_str
from disco.utils.projects import get_project_by_name
from disco.utils.tunnels import TUNNEL_CMD, monitor_tunnel

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


class CreateTunnelReqBody(BaseModel):
    project: str
    service: str


@router.post("/api/tunnels", status_code=201)
async def tunnels_post(req_body: CreateTunnelReqBody):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, req_body.project)
        if project is None:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error", "Project name not found"
                                ),
                                loc=("body", "project"),
                                input=req_body.project,
                            )
                        ],
                    )
                ).errors()
            )

        deployment = await get_live_deployment(dbsession, project)
        if deployment is None:
            raise HTTPException(422, "Project does not have an active deployment")
        disco_file = get_disco_file_from_str(deployment.disco_file)
        if req_body.service not in disco_file.services:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    f"Service not found in {list(disco_file.services)}",
                                ),
                                loc=("body", "service"),
                                input=req_body.service,
                            )
                        ],
                    )
                ).errors()
            )
        service = disco_file.services[req_body.service]
        host = (
            f"{req_body.project}-{req_body.service}"
            if service.exposed_internally
            else docker.service_name(
                req_body.project,
                req_body.service,
                deployment.number,
            )
        )
    tunnel_cmd = TUNNEL_CMD.copy()
    port = random.randint(10000, 65535)
    password = token_hex(16)
    tunnel_service_name = f"disco-tunnel-{port}"
    assert tunnel_cmd[4] == "{name}"
    assert tunnel_cmd[6] == "PASSWORD={password}"
    assert tunnel_cmd[8] == "published={host_port},target=22,protocol=tcp"
    tunnel_cmd[4] = tunnel_cmd[4].format(name=tunnel_service_name)
    tunnel_cmd[6] = tunnel_cmd[6].format(password=password)
    tunnel_cmd[8] = tunnel_cmd[8].format(host_port=port)
    await monitor_tunnel(tunnel_service_name)
    process = await asyncio.create_subprocess_exec(
        *tunnel_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    async def read_stdout() -> None:
        assert process.stdout is not None
        async for line in process.stdout:
            log.info(line.decode("utf-8")[:-1])

    async def read_stderr() -> None:
        assert process.stderr is not None
        async for line in process.stderr:
            log.info(line.decode("utf-8")[:-1])

    tasks = [
        asyncio.create_task(read_stdout()),
        asyncio.create_task(read_stderr()),
    ]
    timeout = 20
    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(*tasks)
    except TimeoutError:
        process.terminate()
        raise Exception(f"Running command failed, timeout after {timeout} seconds")

    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return {
        "tunnel": {
            "host": host,
            "password": password,
            "port": port,
        }
    }
