import asyncio
import logging
import os
import shutil
from pathlib import Path

import aiofiles.os

log = logging.getLogger(__name__)


async def rmtree(path: str) -> None:
    def inner_rmtree() -> None:
        shutil.rmtree(path)

    await asyncio.get_event_loop().run_in_executor(None, inner_rmtree)


async def path_unlink(path: str, missing_ok: bool = False) -> None:
    def inner_path_unlink() -> None:
        f = Path(path)
        f.unlink(missing_ok=missing_ok)

    await asyncio.get_event_loop().run_in_executor(None, inner_path_unlink)


def projects_root() -> str:
    return "/disco/projects"


def project_path(project_name: str) -> str:
    return f"/disco/projects/{project_name}"


def project_path_on_host(host_home: str, project_name: str) -> str:
    return f"{host_home}{project_path(project_name)}"


async def project_folder_exists(project_name: str):
    return await aiofiles.os.path.isdir(project_path(project_name))


async def read_disco_file(
    project_name: str, disco_json_path: str = "disco.json"
) -> str | None:
    path = f"{project_path(project_name)}/{disco_json_path}"
    log.info("Reading disco file %s", path)
    if not await aiofiles.os.path.isfile(path):
        log.info("Disco file does not exist, not reading %s", path)
        return None
    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        return await f.read()


def static_sites_root() -> str:
    return "/disco/srv"


def static_site_deployments_path(project_name: str) -> str:
    return f"/disco/srv/{project_name}"


def static_site_deployment_path(project_name: str, deployment_number: int) -> str:
    return f"{static_site_deployments_path(project_name)}/{deployment_number}"


def static_site_deployment_path_host_machine(
    host_home: str, project_name: str, deployment_number: int
) -> str:
    path = static_site_deployment_path(project_name, deployment_number)
    return f"{host_home}{path}"


async def create_static_site_deployment_directory(
    host_home: str, project_name: str, deployment_number: int
) -> str:
    path = static_site_deployment_path(project_name, deployment_number)
    await aiofiles.os.makedirs(path)
    return static_site_deployment_path_host_machine(
        host_home, project_name, deployment_number
    )


async def remove_project_static_deployments_if_any(project_name: str) -> None:
    path = static_site_deployments_path(project_name)
    if await aiofiles.os.path.isdir(path):
        await rmtree(path)


def static_site_src_public_path(project_name: str, public_path: str) -> str:
    path = os.path.abspath(f"{project_path(project_name)}/{public_path}")
    if not path.startswith(f"{project_path(project_name)}/"):
        # prevent traversal attacks
        raise Exception("publicPath must be inside project folder")
    return path


async def copy_static_site_src_to_deployment_folder(
    project_name: str, public_path: str, deployment_number: int
) -> None:
    src_path = static_site_src_public_path(project_name, public_path)
    dst_path = static_site_deployment_path(project_name, deployment_number)

    def copytree_sync():
        shutil.copytree(src_path, dst_path)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, copytree_sync)


CADDY_CERTIFICATES_DIR = "/disco/caddy/data/caddy/certificates"
ISSUER_REGEX = r"^[A-Za-z0-9_\-][A-Za-z0-9._\-]*$"


def _certificate_directory(issuer: str, domain: str) -> str:
    return f"{CADDY_CERTIFICATES_DIR}/{issuer}/{domain}"


async def get_caddy_certificate_issuer(domain: str) -> str:
    def find_issuer() -> str:
        candidates: list[tuple[float, str]] = []
        for issuer in os.listdir(CADDY_CERTIFICATES_DIR):
            crt_path = f"{_certificate_directory(issuer, domain)}/{domain}.crt"
            if os.path.isfile(crt_path):
                candidates.append((os.path.getmtime(crt_path), issuer))
        if len(candidates) == 0:
            raise FileNotFoundError(f"No certificate found for {domain}")
        candidates.sort()
        return candidates[-1][1]

    return await asyncio.get_running_loop().run_in_executor(None, find_issuer)


async def get_caddy_key_crt(issuer: str, domain: str) -> str:
    path = f"{_certificate_directory(issuer, domain)}/{domain}.crt"
    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        return await f.read()


async def get_caddy_key_key(issuer: str, domain: str) -> str:
    path = f"{_certificate_directory(issuer, domain)}/{domain}.key"
    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        return await f.read()


async def get_caddy_key_meta(issuer: str, domain: str) -> str:
    path = f"{_certificate_directory(issuer, domain)}/{domain}.json"
    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        return await f.read()


async def set_caddy_certificate(
    issuer: str, domain: str, crt: str, key: str, meta: str
) -> None:
    directory = _certificate_directory(issuer, domain)

    def makedirs() -> None:
        os.makedirs(directory, exist_ok=True)

    await asyncio.get_event_loop().run_in_executor(None, makedirs)
    for extension, value in (("crt", crt), ("key", key), ("json", meta)):
        async with aiofiles.open(
            f"{directory}/{domain}.{extension}", "w", encoding="utf-8"
        ) as f:
            await f.write(value)
