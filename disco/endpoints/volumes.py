import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse

from disco import config
from disco.auth import get_api_key_wo_tx
from disco.endpoints.dependencies import get_project_name_from_url_wo_tx
from disco.models.db import ReadSession
from disco.utils import docker
from disco.utils.apikeys import get_api_key_by_id
from disco.utils.deployments import get_live_deployment
from disco.utils.discofile import get_disco_file_from_str
from disco.utils.projects import get_project_by_name, volume_name_for_project

log = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/api/projects/{project_name}/volumes",
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def volumes_get(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
):
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        deployment = await get_live_deployment(dbsession, project)
        volume_names = []
        if deployment is not None:
            disco_file = get_disco_file_from_str(deployment.disco_file)
            for service in disco_file.services.values():
                for volume in service.volumes:
                    volume_names.append(volume.name)
    return {"volumes": [{"name": name} for name in volume_names]}


@router.get("/api/projects/{project_name}/volumes/{volume_name}")
async def volume_get(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    volume_name: str,
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
):
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        deployment = await get_live_deployment(dbsession, project)
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
        source = volume_name_for_project(volume_name, project.id)

    async def iterfile():
        process = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--rm",
            "--workdir",
            "/volume",
            "--mount",
            f"type=volume,source={source},destination=/volume",
            "--label",
            "disco.log.exclude=true",
            f"busybox:{config.BUSYBOX_VERSION}",
            "tar",
            "--create",
            "--gzip",
            "--file",
            "-",
            ".",
            stdout=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        try:
            async for chunk in process.stdout:
                yield chunk
        finally:
            await process.wait()

    return StreamingResponse(iterfile(), media_type="application/x-tar")


@router.put("/api/projects/{project_name}/volumes/{volume_name}")
async def volume_set(
    project_name: Annotated[str, Path()],
    volume_name: Annotated[str, Path()],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    request: Request,
):
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
        project_id = project.id
        deployment = await get_live_deployment(dbsession, project)
        volume_names = []
        if deployment is not None:
            disco_file = get_disco_file_from_str(deployment.disco_file)
            for service in disco_file.services.values():
                for volume in service.volumes:
                    volume_names.append(volume.name)
        if volume_name not in volume_names:
            raise HTTPException(status_code=404)
        assert deployment is not None
        deployment_number = deployment.number
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        api_key_log = api_key.log()

    assert disco_file is not None
    log.info(
        "Importing volume for project %s %s by %s",
        project_name,
        volume_name,
        api_key_log,
    )
    previous_scale = dict(
        [
            (
                docker.service_name(project_name, service.name, deployment_number),
                service.replicas,
            )
            for service in await docker.list_services_for_deployment(
                project_name=project_name,
                deployment_number=deployment_number,
            )
        ],
    )
    services_with_volume = []
    for service_name, service in disco_file.services.items():
        if volume_name not in [v.name for v in service.volumes]:
            continue
        internal_service_name = docker.service_name(
            project_name, service_name, deployment_number
        )
        services_with_volume.append(internal_service_name)
    running_services = [
        service for service in services_with_volume if service in previous_scale
    ]
    scale_zero = dict([(service, 0) for service in running_services])
    scale_back = dict(
        [(service, previous_scale[service]) for service in running_services]
    )
    if len(scale_zero) > 0:
        await docker.scale(scale_zero)
    internal_volume_name = volume_name_for_project(volume_name, project_id)
    attempts = 200
    try:
        for i in range(attempts):
            process = await asyncio.create_subprocess_exec(
                "docker",
                "volume",
                "rm",
                internal_volume_name,
            )
            await process.wait()
            if process.returncode == 0:
                break
            log.info("Failed to remove volume, attempt %d/%d", i + 1, attempts)
            if i + 1 == attempts:
                raise Exception("Error removing previous volume")
            await asyncio.sleep(0.2)
        log.info("Removed %s", internal_volume_name)
        log.info("Receiving file")
        process = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--rm",
            "--interactive",  # to read from stdin
            "--workdir",
            "/volume",
            "--mount",
            f"type=volume,source={internal_volume_name},destination=/volume",
            "--label",
            "disco.log.exclude=true",
            f"busybox:{config.BUSYBOX_VERSION}",
            "tar",
            "--extract",
            "--gzip",
            "--file",
            "-",
            stdin=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        async for chunk in request.stream():
            process.stdin.write(chunk)
            await process.stdin.drain()
        await process.communicate()
        if process.returncode != 0:
            raise Exception("Error receving file")
        log.info("Reveived file")
    finally:
        if len(scale_back) > 0:
            await docker.scale(scale_back)
    log.info("Done importing volume %s", volume_name)
    return {}
