"""Removes the directories of old static deployments.

Each deployment of a static (or generator) site copies the files to
/disco/srv/{project}/{deployment number} (see filesystem.py) and nothing
removed them before the project itself was. Every hour, for each project
that isn't deploying, this keeps the live deployment, the one before it
and whatever Caddy serves, and removes the rest.
"""

import asyncio
import logging
import os
import uuid

import aiofiles.os
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as DBSession

from disco.models import Deployment, Project
from disco.models.db import ReadSession
from disco.utils import caddy
from disco.utils.deployments import DEPLOYMENT_STATUS
from disco.utils.filesystem import (
    removable_static_deployment_numbers,
    rmtree,
    static_site_deployment_path,
    static_site_deployments_path,
    static_sites_root,
)
from disco.utils.projects import get_project_by_name
from disco.utils.worker import worker

log = logging.getLogger(__name__)

# a deployment is being worked on, or will be (the same statuses
# cancel_deployment accepts). The recovery of a failed or cancelled
# deployment runs after the status says FAILED or CANCELLING, so the task
# still running is checked too.
DEPLOYING_STATUSES: list[DEPLOYMENT_STATUS] = [
    "QUEUED",
    "PREPARING",
    "REPLACING",
    "CANCELLING",
]
MAX_REMOVALS_PER_RUN = 100

_lock = asyncio.Lock()


def trash_path() -> str:
    return f"{static_sites_root()}/.trash"


async def remove_old_static_deployments() -> None:
    if _lock.locked():
        log.info("Previous cleanup of static deployments still running, skipping")
        return
    async with _lock:
        await _remove_old_static_deployments()


async def _remove_old_static_deployments() -> None:
    log.info("Cleaning up old static deployments")
    try:
        entries = sorted(await aiofiles.os.listdir(static_sites_root()))
    except FileNotFoundError:
        return
    await _empty_trash()
    removed = 0
    for entry in entries:
        if entry == os.path.basename(trash_path()):
            continue
        if removed >= MAX_REMOVALS_PER_RUN:
            log.info(
                "Removed %d static deployment directories, rest next time", removed
            )
            break
        removed += await _clean_up_project(entry, MAX_REMOVALS_PER_RUN - removed)
    log.info("Done cleaning up old static deployments (%d removed)", removed)


async def _clean_up_project(project_name: str, limit: int) -> int:
    """Removes up to `limit` old deployment directories of the project.

    Returns how many were removed.

    """
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            log.info(
                "No project %s, leaving %s alone",
                project_name,
                static_site_deployments_path(project_name),
            )
            return 0
        project_id = project.id
        if await _is_deploying(dbsession, project):
            return 0
        complete = await _complete_deployment_numbers(dbsession, project)
    if len(complete) == 0:
        return 0
    # a connection error here stops the whole run: nothing is removed
    # without knowing what Caddy serves
    served_root = await caddy.get_static_site_root(project_name)
    try:
        entries = await aiofiles.os.listdir(static_site_deployments_path(project_name))
    except FileNotFoundError:
        return 0  # the project was removed meanwhile
    numbers = removable_static_deployment_numbers(
        entries=entries,
        live=complete[0],
        keep=set(complete),
        served_root=served_root,
        project_name=project_name,
    )
    if len(numbers) == 0:
        return 0
    os.makedirs(trash_path(), exist_ok=True)
    removed = 0
    for number in numbers[:limit]:
        async with ReadSession.begin() as dbsession:
            project = await get_project_by_name(dbsession, project_name)
            if (
                project is None
                or project.id != project_id
                or await _is_deploying(dbsession, project)
            ):
                log.info("Project %s changed, stopping its cleanup", project_name)
                break
        path = static_site_deployment_path(project_name, number)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        # synchronous on purpose: nothing runs between the checks above and
        # the directory being out of the way, so a deployment of a project
        # recreated with the same name can't be writing to it.
        trash = f"{trash_path()}/{project_name}-{number}-{uuid.uuid4().hex}"
        try:
            os.rename(path, trash)
        except FileNotFoundError:
            continue  # the project was removed meanwhile
        log.info("Removing static deployment directory %s", path)
        await _rmtree(trash)
        removed += 1
    return removed


async def _is_deploying(dbsession: DBSession, project: Project) -> bool:
    stmt = (
        select(Deployment.id)
        .where(Deployment.project == project)
        .where(Deployment.status.in_(DEPLOYING_STATUSES))
        .limit(1)
    )
    if (await dbsession.execute(stmt)).first() is not None:
        return True
    running_task_ids = worker.running_task_ids()
    if len(running_task_ids) == 0:
        return False
    stmt = (
        select(Deployment.id)
        .where(Deployment.project == project)
        .where(Deployment.task_id.in_(running_task_ids))
        .limit(1)
    )
    return (await dbsession.execute(stmt)).first() is not None


async def _complete_deployment_numbers(
    dbsession: DBSession, project: Project
) -> list[int]:
    """The live deployment's number and the one before it, newest first."""
    stmt = (
        select(Deployment.number)
        .where(Deployment.project == project)
        .where(Deployment.status == "COMPLETE")
        .order_by(Deployment.number.desc())
        .limit(2)
    )
    result = await dbsession.execute(stmt)
    return list(result.scalars().all())


async def _empty_trash() -> None:
    # what a previous run moved out of the way and didn't finish removing
    os.makedirs(trash_path(), exist_ok=True)
    for entry in await aiofiles.os.listdir(trash_path()):
        await _rmtree(f"{trash_path()}/{entry}")


async def _rmtree(path: str) -> None:
    try:
        await rmtree(path)
    except OSError:
        log.exception("Failed to remove %s", path)
