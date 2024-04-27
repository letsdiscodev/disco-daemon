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
from disco.utils import docker, keyvalues
from disco.utils.apikeys import get_api_key_by_id
from disco.utils.deployments import get_live_deployment
from disco.utils.discofile import get_disco_file_from_str
from disco.utils.encryption import decrypt
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
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    request: Request,
):
    with Session.begin() as dbsession:
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
        assert deployment is not None
        deployment_number = deployment.number
        domain_name = deployment.domain
        commit_hash = deployment.commit_hash
        registry_host = deployment.registry_host
        env_variables = [
            (env_var.name, decrypt(env_var.value))
            for env_var in deployment.env_variables
        ]
        disco_host = keyvalues.get_value(dbsession, "DISCO_HOST")
        assert disco_host is not None
        api_key = get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        api_key_log = api_key.log()

    assert disco_file is not None
    log.info(
        "Importing volume for project %s %s by %s",
        project_name,
        volume_name,
        api_key_log,
    )
    for service_name, service in disco_file.services.items():
        if volume_name not in [v.name for v in service.volumes]:
            continue
        internal_service_name = docker.service_name(
            project_name, service_name, deployment_number
        )
        if not await docker.service_exists_async(internal_service_name):
            log.info("Service %s not running, not trying to stop")
            continue
        log.info("Stopping %s", internal_service_name)
        process = await asyncio.create_subprocess_exec(
            "docker",
            "service",
            "rm",
            internal_service_name,
        )
        await process.wait()
        if process.returncode != 0:
            raise Exception(f"Error stopping service {internal_service_name}")

    source = f"disco-volume-{volume_name}"
    attempts = 200
    for i in range(attempts):
        process = await asyncio.create_subprocess_exec(
            "docker",
            "volume",
            "rm",
            source,
        )
        await process.wait()
        if process.returncode == 0:
            break
        log.info("Failed to remove volume, attempt %d/%d", i + 1, attempts)
        if i + 1 == attempts:
            raise Exception("Error removing previous volume")
        await asyncio.sleep(0.2)
    log.info("Removed %s", source)
    log.info("Receiving file")
    process = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "--interactive",  # to read from stdin
        "--workdir",
        "/volume",
        "--mount",
        f"type=volume,source={source},destination=/volume",
        "busybox",
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
    for service_name, service in disco_file.services.items():
        if volume_name not in [v.name for v in service.volumes]:
            continue
        internal_service_name = docker.service_name(
            project_name, service_name, deployment_number
        )
        # TODO refactor, this code is pretty much copy-pasted from the deployment flow
        networks = [docker.deployment_network_name(project_name, deployment_number)]
        if service_name == "web":
            networks.append(
                docker.deployment_web_network_name(project_name, deployment_number)
            )
        internal_service_name = docker.service_name(
            project_name, service_name, deployment_number
        )
        env_variables += [
            ("DISCO_PROJECT_NAME", project_name),
            ("DISCO_SERVICE_NAME", service_name),
            ("DISCO_HOST", disco_host),
        ]
        if domain_name is not None:
            env_variables += [
                ("DISCO_PROJECT_DOMAIN", domain_name),
            ]
        if commit_hash is not None:
            env_variables += [
                ("DISCO_COMMIT", commit_hash),
            ]

        image = docker.get_image_name_for_service(
            disco_file=disco_file,
            service_name=service_name,
            registry_host=registry_host,
            project_name=project_name,
            deployment_number=deployment_number,
        )
        log.info("Starting %s", internal_service_name)
        await docker.start_service_async(
            image=image,
            name=internal_service_name,
            project_name=project_name,
            project_service_name=service_name,
            deployment_number=deployment_number,
            env_variables=env_variables,
            volumes=[("volume", v.name, v.destination_path) for v in service.volumes],
            published_ports=[
                (p.published_as, p.from_container_port, p.protocol)
                for p in service.published_ports
            ],
            networks=networks,
            replicas=1,  # TODO set same number that was running before removing
            command=service.command,
        )
    log.info("Done importing volume %s", volume_name)
    return {}
