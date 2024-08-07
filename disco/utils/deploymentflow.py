from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass
from typing import Awaitable, Callable

from disco.models import Deployment
from disco.models.db import Session
from disco.utils import caddy, commandoutputs, docker, github, keyvalues
from disco.utils.asyncworker import async_worker
from disco.utils.deployments import (
    DEPLOYMENT_STATUS,
    get_deployment_by_id,
    get_deployment_in_progress,
    get_last_deployment,
    get_live_deployment_sync,
    set_deployment_commit_hash,
    set_deployment_disco_file,
    set_deployment_status,
)
from disco.utils.discofile import DiscoFile, ServiceType, get_disco_file_from_str
from disco.utils.encryption import decrypt
from disco.utils.filesystem import (
    copy_static_site_src_to_deployment_folder,
    create_static_site_deployment_directory,
    project_folder_exists,
    project_path_on_host,
    read_disco_file,
    static_site_deployment_path,
)
from disco.utils.projects import get_project_by_id, volume_name_for_project

log = logging.getLogger(__name__)


class DiscoBuildException(Exception):
    pass


@dataclass
class DeploymentInfo:
    id: str
    number: int
    status: str
    commit_hash: str | None
    disco_file: DiscoFile | None
    project_id: str
    project_name: str
    github_repo_full_name: str | None
    branch: str | None
    registry_host: str | None
    host_home: str
    disco_host: str
    env_variables: list[tuple[str, str]]

    @staticmethod
    def from_deployment(
        deployment: Deployment,
        host_home: str,
        disco_host: str,
    ) -> DeploymentInfo:
        return DeploymentInfo(
            id=deployment.id,
            number=deployment.number,
            status=deployment.status,
            commit_hash=deployment.commit_hash,
            project_id=deployment.project_id,
            project_name=deployment.project_name,
            github_repo_full_name=deployment.github_repo_full_name,
            branch=deployment.branch,
            registry_host=deployment.registry_host,
            host_home=host_home,
            disco_host=disco_host,
            disco_file=get_disco_file_from_str(deployment.disco_file)
            if deployment.disco_file is not None
            else None,
            env_variables=[
                (env_var.name, decrypt(env_var.value))
                for env_var in deployment.env_variables
            ],
        )


def process_deployment(deployment_id: str) -> None:
    from disco.utils.mq.tasks import enqueue_task_deprecated

    async def log_output_async(output: str) -> None:
        log.info("Deployment %s: %s", deployment_id, output)
        await commandoutputs.store_output(
            commandoutputs.deployment_source(deployment_id), output
        )

    def log_output(output: str) -> None:
        asyncio.run(log_output_async(output))

    def log_output_terminate():
        async def async_log_output():
            await commandoutputs.terminate(
                commandoutputs.deployment_source(deployment_id)
            )

        asyncio.run(async_log_output())

    def set_current_deployment_status(status: DEPLOYMENT_STATUS) -> None:
        with Session.begin() as dbsession:
            deployment = get_deployment_by_id(dbsession, deployment_id)
            assert deployment is not None
            set_deployment_status(deployment, status)

    with Session.begin() as dbsession:
        deployment = get_deployment_by_id(dbsession, deployment_id)
        assert deployment is not None
        deployment_in_progress = get_deployment_in_progress(
            dbsession, deployment.project
        )
        if deployment_in_progress is not None:
            log_output(
                f"Deployment {deployment_in_progress.number} in progress, "
                "waiting for build to complete "
                f"before processing deployment {deployment.number}.\n"
            )
            return
        last_deployment = get_last_deployment(dbsession, deployment.project)
        if last_deployment is not None and last_deployment.id != deployment_id:
            log_output(
                f"Deployment {last_deployment.number} is latest, "
                f"skipping deployment {deployment.number}.\n"
            )
            set_current_deployment_status("SKIPPED")
            log_output_terminate()
            enqueue_task_deprecated(
                task_name="PROCESS_DEPLOYMENT_IF_ANY",
                body={
                    "project_id": deployment.project_id,
                },
            )
            return

    set_current_deployment_status("IN_PROGRESS")
    log_output("Starting deployment\n")
    try:
        with Session.begin() as dbsession:
            log.info("Getting data from database for deployment %s", deployment_id)
            deployment = get_deployment_by_id(dbsession, deployment_id)
            assert deployment is not None
            prev_deployment = get_live_deployment_sync(dbsession, deployment.project)
            prev_deployment_id = (
                prev_deployment.id if prev_deployment is not None else None
            )
    except Exception:
        log.exception("Deployment %s failed", deployment_id)
        log_output("Deployment failed\n")
        set_current_deployment_status("FAILED")
        raise
    try:
        replace_deployment(
            new_deployment_id=deployment_id,
            prev_deployment_id=prev_deployment_id,
            recovery=False,
            log_output=log_output,
            log_output_async=log_output_async,
        )
        log_output(f"Deployment complete {random.choice(['🪩', '🕺', '💃'])}\n")
        set_current_deployment_status("COMPLETE")
    except Exception:
        set_current_deployment_status("FAILED")
        log.exception("Exception while deploying")
        log_output("Deployment failed\n")
        log_output("Restoring previous deployment\n")
        replace_deployment(
            new_deployment_id=prev_deployment_id,
            prev_deployment_id=deployment_id,
            recovery=True,
            log_output=log_output,
            log_output_async=log_output_async,
        )
    finally:
        log.info("Finished processing build %s", deployment_id)
        log_output_terminate()

    with Session.begin() as dbsession:
        deployment = get_deployment_by_id(dbsession, deployment_id)
        assert deployment is not None
        enqueue_task_deprecated(
            task_name="PROCESS_DEPLOYMENT_IF_ANY",
            body={
                "project_id": deployment.project_id,
            },
        )


def replace_deployment(
    new_deployment_id: str | None,
    prev_deployment_id: str | None,
    recovery: bool,
    log_output: Callable[[str], None],
    log_output_async: Callable[[str], Awaitable[None]],
):
    log.info(
        "Starting replacement process of deployment %s with %s (recovery: %s)",
        prev_deployment_id,
        new_deployment_id,
        str(recovery),
    )
    new_deployment_info, prev_deployment_info = get_deployment_info(
        new_deployment_id, prev_deployment_id
    )
    if not recovery:
        assert new_deployment_info is not None
        if new_deployment_info.commit_hash is not None:
            checkout_commit(new_deployment_info, log_output)
        elif new_deployment_info.github_repo_full_name is not None:
            new_deployment_info.commit_hash = github.get_head_commit_hash(
                new_deployment_info.project_name
            )
        if new_deployment_info.disco_file is None:
            new_deployment_info.disco_file = read_disco_file_for_deployment(
                new_deployment_info, log_output
            )
        assert new_deployment_info.disco_file is not None
        images = build_images(
            new_deployment_info=new_deployment_info,
            log_output=log_output,
            log_output_async=log_output_async,
        )
        if new_deployment_info.registry_host is not None:
            push_images(images, log_output)
        if "web" in new_deployment_info.disco_file.services:
            if (
                new_deployment_info.disco_file.services["web"].type
                == ServiceType.static
            ):
                prepare_static_site(new_deployment_info, log_output)
            elif (
                new_deployment_info.disco_file.services["web"].type
                == ServiceType.generator
            ):
                prepare_generator_site(new_deployment_info, log_output)
        if (
            "hook:deploy:start:before" in new_deployment_info.disco_file.services
            and new_deployment_info.disco_file.services["hook:deploy:start:before"].type
            == ServiceType.command
        ):
            log_output("Runnning hook:deploy:start:before command\n")
            service_name = "hook:deploy:start:before"
            service = new_deployment_info.disco_file.services[service_name]
            image = docker.get_image_name_for_service(
                disco_file=new_deployment_info.disco_file,
                service_name=service_name,
                registry_host=new_deployment_info.registry_host,
                project_name=new_deployment_info.project_name,
                deployment_number=new_deployment_info.number,
            )
            env_variables = new_deployment_info.env_variables + [
                ("DISCO_PROJECT_NAME", new_deployment_info.project_name),
                ("DISCO_SERVICE_NAME", service_name),
                ("DISCO_HOST", new_deployment_info.disco_host),
            ]
            if new_deployment_info.commit_hash is not None:
                env_variables += [
                    ("DISCO_COMMIT", new_deployment_info.commit_hash),
                ]
            volumes = [
                (
                    "volume",
                    volume_name_for_project(v.name, new_deployment_info.project_id),
                    v.destination_path,
                )
                for v in new_deployment_info.disco_file.services[service_name].volumes
            ]
            docker.run_sync(
                image=image,
                project_name=new_deployment_info.project_name,
                name=f"{new_deployment_info.project_name}-hook-deploy-start-before.{new_deployment_info.number}",
                env_variables=env_variables,
                volumes=volumes,
                networks=["disco-main"],
                command=service.command,
                timeout=service.timeout,
                log_output=log_output,
            )
    if prev_deployment_info is not None:
        async_worker.pause_project_crons(prev_deployment_info.project_name)
    if new_deployment_info is not None:
        assert new_deployment_info.disco_file is not None
        create_networks(new_deployment_info, recovery, log_output)
        stop_conflicting_port_services(
            new_deployment_info, prev_deployment_info, recovery, log_output
        )
        start_services(new_deployment_info, recovery, log_output)
        if "web" in new_deployment_info.disco_file.services:
            with Session.begin() as dbsession:
                project = get_project_by_id(dbsession, new_deployment_info.project_id)
                assert project is not None
                has_domains = len(project.domains) > 0
            if has_domains:
                serve_new_deployment(new_deployment_info, recovery, log_output)
        async_worker.reload_and_resume_project_crons(
            prev_project_name=prev_deployment_info.project_name
            if prev_deployment_info is not None
            else None,
            project_name=new_deployment_info.project_name,
            deployment_number=new_deployment_info.number,
        )
    stop_prev_services(new_deployment_info, prev_deployment_info, recovery, log_output)
    if new_deployment_info is not None:
        remove_unused_networks(new_deployment_info)


def get_deployment_info(
    new_deployment_id: str | None, prev_deployment_id: str | None
) -> tuple[DeploymentInfo | None, DeploymentInfo | None]:
    with Session.begin() as dbsession:
        disco_host = keyvalues.get_value_sync(dbsession, "DISCO_HOST")
        host_home = keyvalues.get_value_sync(dbsession, "HOST_HOME")
        assert disco_host is not None
        assert host_home is not None
        if new_deployment_id is not None:
            new_deployment = get_deployment_by_id(dbsession, new_deployment_id)
            if new_deployment is not None:
                new_deployment_info = DeploymentInfo.from_deployment(
                    deployment=new_deployment,
                    host_home=host_home,
                    disco_host=disco_host,
                )
        else:
            new_deployment_info = None
        if prev_deployment_id is not None:
            prev_deployment = get_deployment_by_id(dbsession, prev_deployment_id)
            assert prev_deployment is not None
            prev_deployment_info = DeploymentInfo.from_deployment(
                prev_deployment,
                host_home=host_home,
                disco_host=disco_host,
            )
        else:
            prev_deployment_info = None
    return new_deployment_info, prev_deployment_info


def checkout_commit(
    new_deployment_info: DeploymentInfo,
    log_output: Callable[[str], None],
) -> None:
    assert new_deployment_info.commit_hash is not None
    assert new_deployment_info.github_repo_full_name is not None
    if not project_folder_exists(new_deployment_info.project_name):
        log_output(f"Cloning github.com/{new_deployment_info.github_repo_full_name}\n")
        try:
            github.clone(
                project_name=new_deployment_info.project_name,
                repo_full_name=new_deployment_info.github_repo_full_name,
            )
        except github.GithubException:
            log_output("Failed to clone repository. Is the repository accessible?\n")
            raise
    else:
        log_output(
            f"Fetching from github.com/{new_deployment_info.github_repo_full_name}\n"
        )
        try:
            github.fetch(
                project_name=new_deployment_info.project_name,
                repo_full_name=new_deployment_info.github_repo_full_name,
            )
        except github.GithubException:
            log_output("Failed to fetch repository. Is the repository accessible?\n")
            raise
    if new_deployment_info.commit_hash == "_DEPLOY_LATEST_":
        github.checkout_latest(
            project_name=new_deployment_info.project_name,
            branch=new_deployment_info.branch,
        )
    else:
        github.checkout_commit(
            new_deployment_info.project_name,
            new_deployment_info.commit_hash,
        )
    commit_hash = github.get_head_commit_hash(new_deployment_info.project_name)
    log_output(f"Checked out commit {commit_hash}\n")
    if new_deployment_info.commit_hash != commit_hash:
        new_deployment_info.commit_hash = commit_hash
        with Session.begin() as dbsession:
            deployment = get_deployment_by_id(dbsession, new_deployment_info.id)
            assert deployment is not None
            set_deployment_commit_hash(deployment, commit_hash)


def read_disco_file_for_deployment(
    new_deployment_info: DeploymentInfo,
    log_output: Callable[[str], None],
) -> DiscoFile | None:
    disco_file_str = read_disco_file(new_deployment_info.project_name)
    if disco_file_str is not None:
        with Session.begin() as dbsession:
            deployment = get_deployment_by_id(dbsession, new_deployment_info.id)
            assert deployment is not None
            set_deployment_disco_file(deployment, disco_file_str)
    else:
        log_output("No disco.json found, falling back to default config\n")
    return get_disco_file_from_str(disco_file_str)


def build_images(
    new_deployment_info: DeploymentInfo,
    log_output: Callable[[str], None],
    log_output_async: Callable[[str], Awaitable[None]],
) -> list[str]:
    assert new_deployment_info.disco_file is not None
    images = []
    env_variables = new_deployment_info.env_variables + [
        ("DISCO_PROJECT_NAME", new_deployment_info.project_name),
        ("DISCO_HOST", new_deployment_info.disco_host),
    ]
    if new_deployment_info.commit_hash is not None:
        env_variables += [
            ("DISCO_COMMIT", new_deployment_info.commit_hash),
        ]
    for service_name, service in new_deployment_info.disco_file.services.items():
        if service.build is None:
            continue
        if service.image is None:
            log_output(
                "Cannot build image of service '%s', missing base 'image' attribute\n",
                service_name,
            )
            raise DiscoBuildException(
                "Discofile service contained 'build' without 'image'"
            )
        log_output(f"Building image for {service_name}\n")
        internal_image_name = docker.internal_image_name(
            registry_host=new_deployment_info.registry_host,
            project_name=new_deployment_info.project_name,
            deployment_number=new_deployment_info.number,
            image_name=service_name,
        )
        images.append(internal_image_name)
        dockerfile_str = docker.easy_mode_dockerfile(service)
        asyncio.run(
            docker.build_image(
                image=internal_image_name,
                project_name=new_deployment_info.project_name,
                dockerfile_str=dockerfile_str,
                context=".",
                env_variables=env_variables,
                stdout=log_output_async,
                stderr=log_output_async,
            )
        )
    for image_name, image in new_deployment_info.disco_file.images.items():
        log_output(f"Building image {image_name}\n")
        internal_image_name = docker.internal_image_name(
            registry_host=new_deployment_info.registry_host,
            project_name=new_deployment_info.project_name,
            deployment_number=new_deployment_info.number,
            image_name=image_name,
        )
        images.append(internal_image_name)
        asyncio.run(
            docker.build_image(
                image=internal_image_name,
                project_name=new_deployment_info.project_name,
                dockerfile_path=image.dockerfile,
                context=image.context,
                env_variables=env_variables,
                stdout=log_output_async,
                stderr=log_output_async,
            )
        )
    return images


def push_images(
    images: list[str],
    log_output: Callable[[str], None],
) -> None:
    for image in images:
        log_output(f"Pushing image to registry: {image}\n")
        docker.push_image(image)


def create_networks(
    new_deployment_info: DeploymentInfo,
    recovery: bool,
    log_output: Callable[[str], None],
) -> None:
    try:
        network_name = docker.deployment_network_name(
            new_deployment_info.project_name, new_deployment_info.number
        )
        if not recovery or not docker.network_exists(network_name):
            docker.create_network(
                network_name,
                project_name=new_deployment_info.project_name,
                deployment_number=new_deployment_info.number,
            )
    except Exception:
        if recovery:
            log.error("Failed to create network %s", network_name)
        else:
            raise


def start_services(
    new_deployment_info: DeploymentInfo,
    recovery: bool,
    log_output: Callable[[str], None],
) -> None:
    assert new_deployment_info.disco_file is not None
    for service_name, service in new_deployment_info.disco_file.services.items():
        if service.type != ServiceType.container:
            continue
        internal_service_name = docker.service_name(
            new_deployment_info.project_name, service_name, new_deployment_info.number
        )
        networks: list[tuple[str, str]] = [
            (
                docker.deployment_network_name(
                    new_deployment_info.project_name, new_deployment_info.number
                ),
                service_name,
            ),
            (
                "disco-main",
                f"{new_deployment_info.project_name}-{service_name}"
                if service.exposed_internally
                else internal_service_name,
            ),
        ]
        env_variables = new_deployment_info.env_variables + [
            ("DISCO_PROJECT_NAME", new_deployment_info.project_name),
            ("DISCO_SERVICE_NAME", service_name),
            ("DISCO_HOST", new_deployment_info.disco_host),
        ]
        if new_deployment_info.commit_hash is not None:
            env_variables += [
                ("DISCO_COMMIT", new_deployment_info.commit_hash),
            ]

        image = docker.get_image_name_for_service(
            disco_file=new_deployment_info.disco_file,
            service_name=service_name,
            registry_host=new_deployment_info.registry_host,
            project_name=new_deployment_info.project_name,
            deployment_number=new_deployment_info.number,
        )
        try:
            if not recovery or not docker.service_exists(internal_service_name):
                log_output(f"Starting service {internal_service_name}\n")
                docker.start_service(
                    image=image,
                    name=internal_service_name,
                    project_name=new_deployment_info.project_name,
                    project_service_name=service_name,
                    deployment_number=new_deployment_info.number,
                    env_variables=env_variables,
                    volumes=[
                        (
                            "volume",
                            volume_name_for_project(
                                v.name, new_deployment_info.project_id
                            ),
                            v.destination_path,
                        )
                        for v in service.volumes
                    ],
                    published_ports=[
                        (p.published_as, p.from_container_port, p.protocol)
                        for p in service.published_ports
                    ],
                    networks=networks,
                    replicas=1,
                    command=service.command,
                )
        except Exception:
            log_output(f"Failed to start service {internal_service_name}\n")
            try:
                service_log = docker.get_log_for_service(
                    service_name=internal_service_name
                )
                log_output(service_log)
            except Exception:
                pass
            if not recovery:
                raise


def stop_conflicting_port_services(
    new_deployment_info: DeploymentInfo,
    prev_deployment_info: DeploymentInfo | None,
    recovery: bool,
    log_output: Callable[[str], None],
) -> None:
    if prev_deployment_info is None:
        return
    assert new_deployment_info.disco_file is not None
    assert prev_deployment_info.disco_file is not None
    new_ports = set()
    for service in new_deployment_info.disco_file.services.values():
        if service.type != ServiceType.container:
            continue
        for port in service.published_ports:
            new_ports.add(port.published_as)

    for service_name, service in prev_deployment_info.disco_file.services.items():
        if service.type != ServiceType.container:
            continue
        conflicts = any(
            [port.published_as in new_ports for port in service.published_ports]
        )
        if not conflicts:
            continue
        internal_service_name = docker.service_name(
            prev_deployment_info.project_name,
            service_name,
            prev_deployment_info.number,
        )
        log_output(
            f"Stopping service {internal_service_name} "
            f"(published port would conflict with replacement service)\n"
        )
        try:
            if not recovery or docker.service_exists(internal_service_name):
                docker.stop_service_sync(internal_service_name)
        except Exception:
            log_output(f"Failed to stop service {internal_service_name}\n")
            if not recovery:
                raise


def serve_new_deployment(
    new_deployment_info: DeploymentInfo,
    recovery: bool,
    log_output: Callable[[str], None],
) -> None:
    assert new_deployment_info.disco_file is not None
    if new_deployment_info.disco_file.services["web"].type == ServiceType.container:
        internal_service_name = docker.service_name(
            new_deployment_info.project_name, "web", new_deployment_info.number
        )
        # TODO wait that it's listening on the port specified? + health check?
        assert new_deployment_info.disco_file is not None
        try:
            if (
                not recovery
                or caddy.get_served_service_for_project(
                    new_deployment_info.project_name
                )
                != internal_service_name
            ):
                log_output(f"Sending HTTP traffic to {internal_service_name}\n")
                caddy.serve_service_sync(
                    new_deployment_info.project_name,
                    internal_service_name,
                    port=new_deployment_info.disco_file.services["web"].port or 8000,
                )
        except Exception:
            log_output(
                f"Failed to update reverse proxy to serve "
                f"deployment {new_deployment_info.number}\n"
            )
            if not recovery:
                raise
    elif new_deployment_info.disco_file.services["web"].type in [
        ServiceType.static,
        ServiceType.generator,
    ]:
        try:
            caddy.serve_static_site_sync(
                new_deployment_info.project_name, new_deployment_info.number
            )
        except Exception:
            log_output(
                f"Failed to update server to serve "
                f"deployment {new_deployment_info.number}\n"
            )
            if not recovery:
                raise
    else:
        raise NotImplementedError(
            f"Deployment type not handled {new_deployment_info.disco_file.services["web"].type}"
        )


def stop_prev_services(
    new_deployment_info: DeploymentInfo | None,
    prev_deployment_info: DeploymentInfo | None,
    recovery: bool,
    log_output: Callable[[str], None],
) -> None:
    if prev_deployment_info is None:
        return
    try:
        if new_deployment_info is None:
            assert recovery
            # just stop everything
            all_services = set(
                docker.list_services_for_project(prev_deployment_info.project_name)
            )
            current_services = set()
        else:
            all_services = set(
                docker.list_services_for_project(new_deployment_info.project_name)
            )
            if prev_deployment_info.project_name != new_deployment_info.project_name:
                all_services |= set(
                    docker.list_services_for_project(prev_deployment_info.project_name)
                )
            current_services = set(
                docker.list_services_for_deployment(
                    new_deployment_info.project_name, new_deployment_info.number
                )
            )
    except Exception:
        log_output("Failed to retrieve list of services to stop\n")
        if not recovery:
            raise

    for service in all_services - current_services:
        try:
            log_output(f"Stopping service {service}\n")
            docker.stop_service_sync(service)
        except Exception:
            log_output(f"Failed to stop service {service}\n")
            if not recovery:
                raise


def remove_unused_networks(
    new_deployment_info: DeploymentInfo,
) -> None:
    try:
        networks_to_keep = docker.list_networks_for_deployment(
            project_name=new_deployment_info.project_name,
            deployment_number=new_deployment_info.number,
        )
        project_networks = docker.list_networks_for_project(
            project_name=new_deployment_info.project_name,
        )
        for network_name in project_networks:
            if network_name not in networks_to_keep:
                try:
                    docker.remove_network(network_name)
                except Exception:
                    log.error("Failed to remove network %s", network_name)
    except Exception:
        log.error("Failed to remove networks")


def prepare_static_site(
    new_deployment_info: DeploymentInfo,
    log_output: Callable[[str], None],
) -> None:
    assert new_deployment_info.disco_file is not None
    assert (
        "web" in new_deployment_info.disco_file.services
        and new_deployment_info.disco_file.services["web"].type == ServiceType.static
    )
    assert new_deployment_info.disco_file.services["web"].public_path is not None
    if new_deployment_info.disco_file.services["web"].command is not None:
        log_output("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n\n")
        log_output('Static site with "command" is deprecated.\n')
        log_output('Use "type": "generator" instead.\n')
        log_output("\n@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n")
        log_output("Runnning static site command\n")
        service_name = "web"
        service = new_deployment_info.disco_file.services[service_name]
        assert service.public_path is not None
        image = docker.get_image_name_for_service(
            disco_file=new_deployment_info.disco_file,
            service_name=service_name,
            registry_host=new_deployment_info.registry_host,
            project_name=new_deployment_info.project_name,
            deployment_number=new_deployment_info.number,
        )
        env_variables = new_deployment_info.env_variables + [
            ("DISCO_PROJECT_NAME", new_deployment_info.project_name),
            ("DISCO_SERVICE_NAME", service_name),
            ("DISCO_HOST", new_deployment_info.disco_host),
            ("DISCO_REPO_PATH", "/repo"),
            ("DISCO_DIST_PATH", service.public_path),
        ]
        if new_deployment_info.commit_hash is not None:
            env_variables += [
                ("DISCO_COMMIT", new_deployment_info.commit_hash),
            ]
        repo_path = project_path_on_host(
            host_home=new_deployment_info.host_home,
            project_name=new_deployment_info.project_name,
        )
        dist_path = create_static_site_deployment_directory(
            host_home=new_deployment_info.host_home,
            project_name=new_deployment_info.project_name,
            deployment_number=new_deployment_info.number,
        )
        volumes = [
            (
                "volume",
                volume_name_for_project(v.name, new_deployment_info.project_id),
                v.destination_path,
            )
            for v in new_deployment_info.disco_file.services[service_name].volumes
        ] + [
            ("bind", repo_path, "/repo"),
            ("bind", dist_path, service.public_path),
        ]
        docker.run_sync(
            image=image,
            project_name=new_deployment_info.project_name,
            name=f"{new_deployment_info.project_name}-build-static-site.{new_deployment_info.number}",
            env_variables=env_variables,
            volumes=volumes,
            networks=[],
            workdir="/repo",
            command=service.command,
            timeout=service.timeout,
            log_output=log_output,
        )
    else:
        log_output("Copying static files\n")
        copy_static_site_src_to_deployment_folder(
            project_name=new_deployment_info.project_name,
            public_path=new_deployment_info.disco_file.services["web"].public_path,
            deployment_number=new_deployment_info.number,
        )


def prepare_generator_site(
    new_deployment_info: DeploymentInfo,
    log_output: Callable[[str], None],
) -> None:
    assert new_deployment_info.disco_file is not None
    assert (
        "web" in new_deployment_info.disco_file.services
        and new_deployment_info.disco_file.services["web"].type == ServiceType.generator
    )
    assert new_deployment_info.disco_file.services["web"].public_path is not None
    image = docker.get_image_name_for_service(
        disco_file=new_deployment_info.disco_file,
        service_name="web",
        registry_host=new_deployment_info.registry_host,
        project_name=new_deployment_info.project_name,
        deployment_number=new_deployment_info.number,
    )
    dst = static_site_deployment_path(
        project_name=new_deployment_info.project_name,
        deployment_number=new_deployment_info.number,
    )
    create_static_site_deployment_directory(
        host_home=new_deployment_info.host_home,
        project_name=new_deployment_info.project_name,
        deployment_number=new_deployment_info.number,
    )
    src = new_deployment_info.disco_file.services["web"].public_path
    if not src.startswith("/"):
        workdir = docker.get_image_workdir(image)
        src = os.path.join(workdir, src)
    log_output(f"Copying static files from Docker image {src}\n")
    docker.copy_files_from_image(
        image=image,
        src=src,
        dst=dst,
    )
