import asyncio
import logging
import subprocess
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key, get_api_key_wo_tx
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.models import ApiKey, Project
from disco.models.db import Session
from disco.utils.deployments import get_live_deployment
from disco.utils.discofile import get_disco_file_from_str
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/projects/{project_name}/volumes", dependencies=[Depends(get_api_key)])
def volumes_get(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
):
    deployment = get_live_deployment(dbsession, project)
    volume_names = []
    if deployment is not None:
        disco_file = get_disco_file_from_str(deployment.disco_file)
        for service in disco_file.services.values():
            for volume in service.volumes:
                volume_names.append(volume.name)
    return {"volumes": [{"name": name} for name in volume_names]}


@router.get("/projects/{project_name}/volumes/{volume_name}")
def volume_get(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    volume_name: str,
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    deployment = get_live_deployment(dbsession, project)
    volume_names = []
    if deployment is not None:
        disco_file = get_disco_file_from_str(deployment.disco_file)
        for service in disco_file.services.values():
            for volume in service.volumes:
                volume_names.append(volume.name)
    if volume_name not in volume_names:
        raise HTTPException(status_code=404)

    log.info(
        "Exporting volume from project %s %s by %s",
        project.name,
        volume_name,
        api_key.log(),
    )

    def iterfile():
        source = f"disco-volume-{volume_name}"
        args = [
            "docker",
            "run",
            "--rm",
            "--workdir",
            "/volume",
            "--mount",
            f"type=volume,source={source},destination=/volume",
            "busybox",
            "tar",
            "--create",
            "--gzip",
            "--file",
            "-",
            ".",
        ]
        with subprocess.Popen(
            args=args,
            stdout=subprocess.PIPE,
        ) as process:
            assert process.stdout is not None
            yield from process.stdout

    return StreamingResponse(iterfile(), media_type="application/x-tar")


@router.put("/projects/{project_name}/volumes/{volume_name}")
async def volume_set(
    project_name: Annotated[str, Path()],
    volume_name: Annotated[str, Path()],
    api_key: Annotated[ApiKey, Depends(get_api_key_wo_tx)],
    request: Request,
):
    with Session() as dbsession:
        with dbsession.begin():
            project = get_project_by_name(dbsession, project_name)
            if project is None:
                raise HTTPException(status_code=404)
            deployment = get_live_deployment(dbsession, project)
            volume_names = []
            if deployment is not None:
                disco_file = get_disco_file_from_str(deployment.disco_file)
                for service in disco_file.services.values():
                    for volume in service.volumes:
                        volume_names.append(volume.name)
            if volume_name not in volume_names:
                raise HTTPException(status_code=404)
    log.info(
        "Importing volume for project %s %s by %s",
        project_name,
        volume_name,
        api_key.log(),
    )
    # TODO remove previous volume with same name if exists
    source = f"disco-volume-{volume_name}-tmp"
    process = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "--workdir",
        "/volume",
        "--mount",
        f"type=volume,source={source},destination=/volume",
        "busybox",
        "tar",
        "--extract",
        "--gunzip",
        "--file",
        "-",
        stdin=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    async for chunk in request.stream():
        process.stdin.write(chunk)
        await process.stdin.drain()
    await process.wait()
    # TODO stop services that use the volume
    # TODO rename volume
    # TODO start services that use the volume
    return {}
