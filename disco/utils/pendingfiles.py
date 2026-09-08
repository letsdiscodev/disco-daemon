import asyncio
import logging
import re
import tarfile
import uuid
from collections.abc import AsyncIterator

import aiofiles
import aiofiles.os
from sqlalchemy import select

from disco.models import Deployment
from disco.models.db import ReadSession
from disco.utils.discofile import DiscoFile
from disco.utils.filesystem import project_path, projects_root, rmtree

log = logging.getLogger(__name__)

PENDING_DIR_RE = re.compile(r"^[a-z][a-z0-9\-]*\.pending-[0-9]+$")
UPLOAD_RE = re.compile(r"^[a-z][a-z0-9\-]*\.upload-[0-9a-f]{32}(\.tar\.gz)?$")


class FilesArchiveError(Exception):
    pass


class PendingFilesNotFound(Exception):
    pass


def pending_path(project_name: str, deployment_number: int) -> str:
    return f"{projects_root()}/{project_name}.pending-{deployment_number}"


def _upload_path(project_name: str) -> str:
    return f"{projects_root()}/{project_name}.upload-{uuid.uuid4().hex}"


async def receive_tar_gz(project_name: str, chunks: AsyncIterator[bytes]) -> str:
    """Extract an uploaded gzipped tar; return the directory it was extracted to."""
    archive_path = f"{_upload_path(project_name)}.tar.gz"
    destination = _upload_path(project_name)
    size = 0
    try:
        async with aiofiles.open(archive_path, "wb") as f:
            async for chunk in chunks:
                size += len(chunk)
                await f.write(chunk)
        log.info("Received files for project %s: %d bytes", project_name, size)

        def extract() -> None:
            with tarfile.open(archive_path, "r:gz") as tar:
                # the "data" filter refuses absolute paths, paths escaping the
                # destination, links pointing outside of it and special files
                tar.extractall(destination, filter="data")

        try:
            await asyncio.get_running_loop().run_in_executor(None, extract)
        except (tarfile.TarError, OSError) as ex:
            if await aiofiles.os.path.isdir(destination):
                await rmtree(destination)
            raise FilesArchiveError(f"Invalid files archive: {ex}") from ex
        return destination
    finally:
        await aiofiles.os.remove(archive_path)


async def write_disco_file(project_name: str, disco_file: DiscoFile) -> str:
    """A directory holding only disco.json; return it."""
    destination = _upload_path(project_name)
    await aiofiles.os.makedirs(destination)
    async with aiofiles.open(f"{destination}/disco.json", "w", encoding="utf-8") as f:
        await f.write(disco_file.model_dump_json(indent=2, by_alias=True))
    return destination


async def set_pending(
    project_name: str, received_path: str, deployment_number: int
) -> None:
    """The received files are now pending for that deployment."""
    await aiofiles.os.rename(
        received_path, pending_path(project_name, deployment_number)
    )


async def set_current(project_name: str, deployment_number: int) -> None:
    """The pending files of the deployment become the project directory."""
    pending = pending_path(project_name, deployment_number)
    if not await aiofiles.os.path.isdir(pending):
        raise PendingFilesNotFound(
            f"No pending files for deployment {deployment_number} of {project_name}"
        )
    log.info(
        "Files of deployment %d become current for project %s",
        deployment_number,
        project_name,
    )
    if await aiofiles.os.path.isdir(project_path(project_name)):
        await rmtree(project_path(project_name))
    await aiofiles.os.rename(pending, project_path(project_name))


async def set_current_as_pending(
    project_name: str, deployment_number: int | None
) -> None:
    """Make project directory pending, as backup in case we need to revert."""
    if not await aiofiles.os.path.isdir(project_path(project_name)):
        return
    if deployment_number is None:
        await rmtree(project_path(project_name))
        return
    pending = pending_path(project_name, deployment_number)
    if await aiofiles.os.path.isdir(pending):
        await rmtree(pending)
    log.info(
        "Project directory of %s becomes pending for deployment %d",
        project_name,
        deployment_number,
    )
    await aiofiles.os.rename(project_path(project_name), pending)


async def remove(project_name: str, deployment_number: int) -> None:
    pending = pending_path(project_name, deployment_number)
    if await aiofiles.os.path.isdir(pending):
        log.info("Removing pending files %s", pending)
        await rmtree(pending)


async def remove_all(project_name: str) -> None:
    for entry in await aiofiles.os.listdir(projects_root()):
        if entry.startswith(f"{project_name}.") and (
            PENDING_DIR_RE.match(entry) or UPLOAD_RE.match(entry)
        ):
            log.info("Removing %s", entry)
            await _remove(f"{projects_root()}/{entry}")


async def clean_up_pending_files_on_disco_boot() -> None:
    # Keeps the pending files of queued deployments, puts back the files
    # pending for a live deployment (the daemon stopped while a new deployment
    # ran; it failed), removes the rest.
    from disco.utils.deployments import get_live_deployment
    from disco.utils.projects import get_all_projects

    entries = await aiofiles.os.listdir(projects_root())
    async with ReadSession.begin() as dbsession:
        stmt = (
            select(Deployment.project_name, Deployment.number)
            .where(Deployment.deployment_type == "FILES")
            .where(Deployment.status == "QUEUED")
        )
        expected = {
            pending_path(project_name, number)
            for project_name, number in (await dbsession.execute(stmt)).all()
        }
        live: list[tuple[str, int]] = []
        for project in await get_all_projects(dbsession):
            live_deployment = await get_live_deployment(dbsession, project)
            if live_deployment is not None:
                live.append((project.name, live_deployment.number))
    for project_name, number in live:
        try:
            await set_current(project_name, number)
        except PendingFilesNotFound:
            pass  # the usual case: no deployment was replacing the directory
    for entry in entries:
        if PENDING_DIR_RE.match(entry) is None and UPLOAD_RE.match(entry) is None:
            continue
        path = f"{projects_root()}/{entry}"
        if path in expected or not await aiofiles.os.path.exists(path):
            continue
        log.info("Removing files left behind %s", path)
        await _remove(path)


async def _remove(path: str) -> None:
    if await aiofiles.os.path.isdir(path):
        await rmtree(path)
    else:
        await aiofiles.os.remove(path)
