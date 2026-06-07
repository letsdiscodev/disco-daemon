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


def create_static_site_deployment_directory_sync(
    host_home: str, project_name: str, deployment_number: int
) -> str:
    path = static_site_deployment_path(project_name, deployment_number)
    os.makedirs(path)
    return static_site_deployment_path_host_machine(
        host_home, project_name, deployment_number
    )


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


# Caddy TLS certs now live in dqlite (the caddy_data table managed by the Caddy
# dqlite storage module), not on a local filesystem volume. Keys mirror Caddy's
# certmagic layout relative to its storage root.
_CERT_STORAGE_PREFIX = "certificates/acme-v02.api.letsencrypt.org-directory"


def _cert_storage_key(domain: str, ext: str) -> str:
    return f"{_CERT_STORAGE_PREFIX}/{domain}/{domain}.{ext}"


async def _get_caddy_data(key: str) -> str:
    from sqlalchemy import text

    from disco.models.db import AsyncReadSession

    async with AsyncReadSession() as dbsession:
        result = await dbsession.execute(
            text("SELECT value FROM caddy_data WHERE key = :k"), {"k": key}
        )
        row = result.first()
    if row is None:
        raise FileNotFoundError(f"Caddy data not found in dqlite: {key}")
    value = row[0]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return str(value)


def _set_caddy_data(key: str, value: str) -> None:
    import time

    from sqlalchemy import text

    from disco.models.db import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO caddy_data(key, value, modified) VALUES (:k, :v, :m) "
                "ON CONFLICT(key) DO UPDATE SET value = :v, modified = :m"
            ),
            {"k": key, "v": value.encode("utf-8"), "m": time.time_ns()},
        )


async def get_caddy_key_crt(domain: str) -> str:
    return await _get_caddy_data(_cert_storage_key(domain, "crt"))


def set_caddy_key_crt(domain: str, value: str) -> None:
    _set_caddy_data(_cert_storage_key(domain, "crt"), value)


async def get_caddy_key_key(domain: str) -> str:
    return await _get_caddy_data(_cert_storage_key(domain, "key"))


def set_caddy_key_key(domain: str, value: str) -> None:
    _set_caddy_data(_cert_storage_key(domain, "key"), value)


async def get_caddy_key_meta(domain: str) -> str:
    return await _get_caddy_data(_cert_storage_key(domain, "json"))


def set_caddy_key_meta(domain: str, value: str) -> None:
    _set_caddy_data(_cert_storage_key(domain, "json"), value)
