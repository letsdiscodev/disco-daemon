import logging
from dataclasses import dataclass
from typing import Sequence

from disco.models import Deployment
from disco.models.db import AsyncSession
from disco.utils import docker
from disco.utils.projects import get_all_projects

log = logging.getLogger(__name__)


async def remove_unused_images() -> None:
    log.info("Cleaning up Docker images")
    images = await docker.ls_images_swarm()
    active_projects = await get_active_projects()
    images_to_remove = [
        (image, tag)
        for image, tag in images
        if should_remove_image(image=image, tag=tag, active_projects=active_projects)
    ]
    for image, tag in images_to_remove:
        if tag == "<none>":
            image_str = image
        else:
            image_str = f"{image}:{tag}"
        log.info("Removing Docker image %s", image_str)
        await docker.rm_image_swarm(image_str)
    log.info("Done cleaning up Docker images")


@dataclass
class ActiveProject:
    project_name: str
    deployment_number: int


def should_remove_image(
    image: str, tag: str, active_projects: list[ActiveProject]
) -> bool:
    if not image.startswith("disco/project-"):
        # we're currently not removing images that Disco may not have added
        return False
    for active_project in active_projects:
        if not image.startswith(f"disco/project-{active_project.project_name}-"):
            continue  # does not match this project
        try:
            deployment_number = int(tag)
        except ValueError:
            continue  # is not a deployment number
        if deployment_number == active_project.deployment_number:
            return False  # project matches, deployment matches
    return True


async def get_active_projects() -> list[ActiveProject]:
    active_projects = []
    async with AsyncSession.begin() as dbsession:
        projects = await get_all_projects(dbsession)
        for project in projects:
            deployments: Sequence[
                Deployment
            ] = await project.awaitable_attrs.deployments
            for deployment in deployments:
                if deployment.status in [
                    "QUEUEUD",
                    "PREPARING",
                    "REPLACING",
                    "COMPLETE",
                ]:
                    # add all deployments that are live or will be live
                    active_project = ActiveProject(
                        project_name=project.name,
                        deployment_number=deployment.number,
                    )
                    active_projects.append(active_project)
                    if deployment.status == "COMPLETE":
                        # but don't go back farther than current live deployment
                        break
    return active_projects
