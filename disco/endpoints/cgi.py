import logging
import re
import uuid
from dataclasses import dataclass
from typing import Annotated, AsyncGenerator

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response

from disco.auth import get_api_key_wo_tx
from disco.endpoints.dependencies import get_project_name_from_url_wo_tx
from disco.models.db import AsyncSession
from disco.utils import docker, keyvalues
from disco.utils.deployments import get_live_deployment
from disco.utils.discofile import ServiceType, get_disco_file_from_str
from disco.utils.encryption import decrypt
from disco.utils.projects import get_project_by_name, volume_name_for_project

log = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/api/projects/{project_name}/cgi/{service_name}",
)
@router.post(
    "/api/projects/{project_name}/cgi/{service_name}",
)
@router.put(
    "/api/projects/{project_name}/cgi/{service_name}",
)
@router.delete(
    "/api/projects/{project_name}/cgi/{service_name}",
)
@router.patch(
    "/api/projects/{project_name}/cgi/{service_name}",
)
async def cgi_root(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    service_name: Annotated[str, Path()],
    request: Request,
    x_disco_include_api_key: Annotated[str | None, Header()] = None,
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        deployment = await get_live_deployment(dbsession, project)
        if deployment is None:
            raise HTTPException(422, "Must deploy first")
        disco_file = get_disco_file_from_str(deployment.disco_file)
    if service_name not in disco_file.services.keys():
        raise HTTPException(
            404, f"Service {service_name} not found in project {project_name}"
        )
    if disco_file.services[service_name].type != ServiceType.cgi:
        raise HTTPException(
            404,
            f"Service {service_name} type is {disco_file.services[service_name].type}, not cgi",
        )

    cgi_response = await request_cgi(
        project_name=project_name,
        service_name=service_name,
        content=request.stream(),
        content_length=request.headers.get("Content-Length"),
        content_type=request.headers.get("Content-Type"),
        path_info="/",
        query_string=request.url.query,
        requet_method=request.method,
        include_api_key=x_disco_include_api_key is not None
        and x_disco_include_api_key.lower() == "true",
        by_api_key_id=api_key_id,
    )
    if cgi_response.status_code == 500:
        raise Exception(f"Error from CGI script:\n{cgi_response.content}")
    return Response(
        content=cgi_response.content,
        status_code=cgi_response.status_code,
        headers=cgi_response.headers,
    )


@router.get(
    "/api/projects/{project_name}/cgi/{service_name}/{path_info:path}",
)
@router.post(
    "/api/projects/{project_name}/cgi/{service_name}/{path_info:path}",
)
@router.put(
    "/api/projects/{project_name}/cgi/{service_name}/{path_info:path}",
)
@router.delete(
    "/api/projects/{project_name}/cgi/{service_name}/{path_info:path}",
)
@router.patch(
    "/api/projects/{project_name}/cgi/{service_name}/{path_info:path}",
)
async def cgi_with_path(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    service_name: Annotated[str, Path()],
    request: Request,
    path_info: Annotated[str, Path()],
    x_disco_include_api_key: Annotated[str | None, Header()] = None,
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        deployment = await get_live_deployment(dbsession, project)
        if deployment is None:
            raise HTTPException(422, "Must deploy first")
        disco_file = get_disco_file_from_str(deployment.disco_file)
    if service_name not in disco_file.services.keys():
        raise HTTPException(
            404, f"Service {service_name} not found in project {project_name}"
        )
    if disco_file.services[service_name].type != ServiceType.cgi:
        raise HTTPException(
            404,
            f"Service {service_name} type is {disco_file.services[service_name].type}, not cgi",
        )

    cgi_response = await request_cgi(
        project_name=project_name,
        service_name=service_name,
        content=request.stream(),
        content_length=request.headers.get("Content-Length"),
        content_type=request.headers.get("Content-Type"),
        path_info=f"/{path_info}",
        query_string=request.url.query,
        requet_method=request.method,
        include_api_key=x_disco_include_api_key is not None
        and x_disco_include_api_key.lower() == "true",
        by_api_key_id=api_key_id,
    )
    if cgi_response.status_code == 500:
        raise Exception(f"Error from CGI script:\n{cgi_response.content}")
    return Response(
        content=cgi_response.content,
        status_code=cgi_response.status_code,
        headers=cgi_response.headers,
    )


@dataclass
class CgiResponse:
    status_code: int
    headers: dict[str, str]
    content: str


async def request_cgi(
    project_name: str,
    service_name: str,
    content: AsyncGenerator[bytes, None],
    content_length: str | None,
    content_type: str | None,
    path_info: str,
    query_string: str,
    requet_method: str,
    by_api_key_id: str,
    include_api_key: bool,
) -> CgiResponse:
    resp_text = ""
    cgi_err = ""

    def stdout(text: str) -> None:
        nonlocal resp_text
        resp_text += text

    def stderr(text: str) -> None:
        nonlocal cgi_err
        cgi_err += text

    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        deployment = await get_live_deployment(dbsession, project)
        assert deployment is not None
        disco_file = get_disco_file_from_str(deployment.disco_file)
        registry_host = await keyvalues.get_value(dbsession, "REGISTRY_HOST")
        image = docker.get_image_name_for_service(
            disco_file=disco_file,
            service_name=service_name,
            registry_host=registry_host,
            project_name=project_name,
            deployment_number=deployment.number,
        )
        command = disco_file.services[service_name].command
        env_variables = [
            (env_var.name, decrypt(env_var.value))
            for env_var in await deployment.awaitable_attrs.env_variables
        ]
        env_variables += [
            ("DISCO_PROJECT_NAME", project_name),
            ("DISCO_SERVICE_NAME", service_name),
            ("DISCO_HOST", await keyvalues.get_value_str(dbsession, "DISCO_HOST")),
        ]
        if include_api_key:
            log.info("Including DISCO_API_KEY env variable")
            env_variables.append(("DISCO_API_KEY", by_api_key_id))
        if deployment.commit_hash is not None:
            env_variables += [
                ("DISCO_COMMIT", deployment.commit_hash),
            ]
        env_variables += cgi_env_variables(
            content_length=content_length,
            content_type=content_type,
            path_info=path_info,
            query_string=query_string,
            requet_method=requet_method,
        ).items()

        network = docker.deployment_network_name(project.name, deployment.number)
        volumes = [
            ("volume", volume_name_for_project(v.name, project.id), v.destination_path)
            for v in disco_file.services[service_name].volumes
        ]
    log.info(
        "Requesting CGI %s %s%s from %s %s",
        requet_method,
        path_info,
        query_string,
        project_name,
        service_name,
    )
    await docker.run(
        image=image,
        project_name=project_name,
        name=f"{project_name}-cgi.{uuid.uuid4().hex}",
        env_variables=env_variables,
        volumes=volumes,
        networks=[network, "disco-main"],
        command=command,
        stdin=content,
        stdout=stdout,
        stderr=stderr,
    )
    if len(cgi_err) > 0:
        log.info("Received stderr from CGI: %s", cgi_err)
    return parse_cgi_response_text(resp_text)


def cgi_env_variables(
    content_length: str | None,
    content_type: str | None,
    path_info: str,
    query_string: str,
    requet_method: str,
) -> dict[str, str]:
    return {
        "AUTH_TYPE": "",
        "CONTENT_LENGTH": content_length if content_length is not None else "",
        "CONTENT_TYPE": content_type if content_type is not None else "",
        "GATEWAY_INTERFACE": "CGI/1.1",
        "PATH_INFO": path_info,
        "PATH_TRANSLATED": "",
        "QUERY_STRING": query_string,
        "REMOTE_ADDR": "",
        "REMOTE_HOST": "",
        "REMOTE_IDENT": "",
        "REMOTE_USER": "",
        "REQUEST_METHOD": requet_method,
        "SCRIPT_NAME": "",
        "SERVER_NAME": "",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "SERVER_SOFTWARE": "Disco",
    }


class CgiResponseException(Exception):
    pass


def parse_cgi_response_text(text: str) -> CgiResponse:
    lines = text.split("\n")
    status_line = lines.pop(0)
    m = re.match(r"^status: (\d{3}) .+$", status_line, flags=re.IGNORECASE)
    try:
        assert m is not None
        status_code = int(m.group(1))
    except Exception:
        raise CgiResponseException(
            f"Couldn't parse status from first line of CGI response: {status_line}\n{text}"
        )
    headers = []
    while len(lines) > 0:
        line = lines.pop(0)
        if len(line.strip()) == 0:
            # empty line, end of headers
            break
        parts = line.split(": ")
        if len(parts) < 2:
            raise CgiResponseException(
                f"Couldn't parse header of CGI response: {line}\n{text}"
            )
        key = parts.pop(0)
        value = ": ".join(parts)
        headers.append((key, value.strip()))
    resp_body = "\n".join(lines)
    return CgiResponse(
        status_code=status_code, headers=dict(headers), content=resp_body
    )
