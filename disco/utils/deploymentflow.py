from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from typing import Callable

from disco.models import Deployment
from disco.models.db import Session
from disco.utils import caddy, commandoutputs, docker, github, keyvalues
from disco.utils.deployments import (
    BUILD_STATUS,
    get_deployment_by_id,
    get_live_deployment,
    set_deployment_commit_hash,
    set_deployment_disco_file,
    set_deployment_status,
)
from disco.utils.discofile import DiscoFile, ServiceType
from disco.utils.filesystem import (
    copy_static_site_src_to_deployment_folder,
    project_folder_exists,
    read_disco_file,
)

log = logging.getLogger(__name__)


@dataclass
class DeploymentInfo:
    id: str
    number: int
    status: str
    commit_hash: str | None
    disco_file: DiscoFile | None
    project_name: str
    github_repo: str | None
    github_host: str | None
    registry_host: str
    disco_host: str
    domain_name: str | None
    env_variables: list[tuple[str, str]]

    @staticmethod
    def from_deployment(
        deployment: Deployment, registry_host: str, disco_host: str
    ) -> DeploymentInfo:
        return DeploymentInfo(
            id=deployment.id,
            number=deployment.number,
            status=deployment.status,
            commit_hash=deployment.commit_hash,
            project_name=deployment.project_name,
            github_repo=deployment.github_repo,
            registry_host=registry_host,
            disco_host=disco_host,
            domain_name=deployment.domain,
            github_host=deployment.github_host,
            disco_file=DiscoFile.model_validate_json(deployment.disco_file)
            if deployment.disco_file is not None
            else None,
            env_variables=[
                (env_var.name, env_var.value) for env_var in deployment.env_variables
            ],
        )


def process_deployment(deployment_id: str) -> None:
    def log_output(output: str | None) -> None:
        if output is not None:
            log.info("Deployment %s: %s", deployment_id, output)
        with Session() as dbsession:
            with dbsession.begin():
                commandoutputs.save(dbsession, f"DEPLOYMENT_{deployment_id}", output)

    def set_current_deployment_status(status: BUILD_STATUS) -> None:
        with Session() as dbsession:
            with dbsession.begin():
                deployment = get_deployment_by_id(dbsession, deployment_id)
                assert deployment is not None
                set_deployment_status(deployment, status)

    set_current_deployment_status("IN_PROGRESS")
    log_output("Starting deployment\n")
    try:
        with Session() as dbsession:
            with dbsession.begin():
                log.info("Getting data from database for deployment %s", deployment_id)
                deployment = get_deployment_by_id(dbsession, deployment_id)
                assert deployment is not None
                prev_deployment = get_live_deployment(dbsession, deployment.project)
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
        )
        log_output("Deployment complete\n")
        set_current_deployment_status("COMPLETE")
    except Exception:
        set_current_deployment_status("FAILED")
        log_output(traceback.format_exc())
        log_output("Deployment failed.\n")
        log_output("Restoring previous deployment\n")
        replace_deployment(
            new_deployment_id=prev_deployment_id,
            prev_deployment_id=deployment_id,
            recovery=True,
            log_output=log_output,
        )
    finally:
        log.info("Finished processing build %s", deployment_id)
        log_output(None)  # end


def replace_deployment(
    new_deployment_id: str | None,
    prev_deployment_id: str | None,
    recovery: bool,
    log_output: Callable[[str], None],
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
        if new_deployment_info.disco_file is None:
            new_deployment_info.disco_file = read_disco_file_for_deployment(
                new_deployment_info, log_output
            )
        assert new_deployment_info.disco_file is not None
        images = build_images(new_deployment_info, log_output)
        push_images(images, log_output)
        if (
            "web" in new_deployment_info.disco_file.services
            and new_deployment_info.disco_file.services["web"].type
            == ServiceType.static
        ):
            prepare_static_site(new_deployment_info, log_output)
    if new_deployment_info is not None:
        assert new_deployment_info.disco_file is not None
        create_networks(new_deployment_info, recovery, log_output)
        stop_conflicting_port_services(
            new_deployment_info, prev_deployment_info, recovery, log_output
        )
        start_services(new_deployment_info, recovery, log_output)
        if (
            "web" in new_deployment_info.disco_file.services
            and new_deployment_info.domain_name is not None
        ):
            serve_new_deployment(new_deployment_info, recovery, log_output)
    stop_prev_services(new_deployment_info, prev_deployment_info, recovery, log_output)
    remove_prev_networks(prev_deployment_info, recovery, log_output)


def get_deployment_info(
    new_deployment_id: str | None, prev_deployment_id: str | None
) -> tuple[DeploymentInfo | None, DeploymentInfo | None]:
    with Session() as dbsession:
        with dbsession.begin():
            disco_host = keyvalues.get_value(dbsession, "DISCO_HOST")
            registry_host = keyvalues.get_value(dbsession, "REGISTRY_HOST")
            assert disco_host is not None
            assert registry_host is not None
            if new_deployment_id is not None:
                new_deployment = get_deployment_by_id(dbsession, new_deployment_id)
                if new_deployment is not None:
                    new_deployment_info = DeploymentInfo.from_deployment(
                        deployment=new_deployment,
                        registry_host=registry_host,
                        disco_host=disco_host,
                    )
            else:
                new_deployment_info = None
            if prev_deployment_id is not None:
                prev_deployment = get_deployment_by_id(dbsession, prev_deployment_id)
                assert prev_deployment is not None
                prev_deployment_info = DeploymentInfo.from_deployment(
                    prev_deployment, registry_host=registry_host, disco_host=disco_host
                )
            else:
                prev_deployment_info = None
    return new_deployment_info, prev_deployment_info


def checkout_commit(
    new_deployment_info: DeploymentInfo,
    log_output: Callable[[str], None],
) -> None:
    log_output(f"Deployment of git {new_deployment_info.commit_hash}\n")
    assert new_deployment_info.github_repo is not None
    assert new_deployment_info.github_host is not None
    assert new_deployment_info.commit_hash is not None
    if not project_folder_exists(new_deployment_info.project_name):
        log_output(f"Cloning project from {new_deployment_info.github_repo}\n")
        github.clone_project(
            project_name=new_deployment_info.project_name,
            github_repo=new_deployment_info.github_repo,
            github_host=new_deployment_info.github_host,
            log_output=log_output,
        )
        # TODO if project doesn't have branch configured,
        #      save if origin/main exists, otherwise, origin/master
    else:
        log_output("Fetching latest commits from git repo\n")
        github.fetch(
            project_name=new_deployment_info.project_name, log_output=log_output
        )
    if new_deployment_info.commit_hash == "_DEPLOY_LATEST_":
        # TODO use project branch
        log_output("Checking out latest commit\n")
        github.checkout_latest(
            new_deployment_info.project_name,
            log_output=log_output,
        )
    else:
        log_output(f"Checking out commit {new_deployment_info.commit_hash}\n")
        github.checkout_commit(
            new_deployment_info.project_name,
            new_deployment_info.commit_hash,
            log_output=log_output,
        )
    commit_hash = github.get_head_commit_hash(new_deployment_info.project_name)
    if new_deployment_info.commit_hash != commit_hash:
        with Session() as dbsession:
            with dbsession.begin():
                deployment = get_deployment_by_id(dbsession, new_deployment_info.id)
                assert deployment is not None
                set_deployment_commit_hash(deployment, commit_hash)


def read_disco_file_for_deployment(
    new_deployment_info: DeploymentInfo,
    log_output: Callable[[str], None],
) -> DiscoFile | None:
    log_output("Reading Disco file from project folder\n")
    disco_file_str = read_disco_file(new_deployment_info.project_name)
    if disco_file_str is None:
        log_output("No disco.json found, falling back to default config\n")
        default_config = dict(
            version="1.0",
            services=dict(
                web=dict(
                    image=dict(
                        dockerfile="Dockerfile",
                        context=".",
                    ),
                    port=8000,
                    command=None,
                )
            ),
        )
        return DiscoFile.model_validate(default_config)
    log_output("Found disco.json\n")
    with Session() as dbsession:
        with dbsession.begin():
            deployment = get_deployment_by_id(dbsession, new_deployment_info.id)
            assert deployment is not None
            set_deployment_disco_file(deployment, disco_file_str)
    return DiscoFile.model_validate_json(disco_file_str)


def build_images(
    new_deployment_info: DeploymentInfo,
    log_output: Callable[[str], None],
) -> list[str]:
    images = set()
    log_output("Building images\n")
    assert new_deployment_info.disco_file is not None
    for service_name, service in new_deployment_info.disco_file.services.items():
        if service.image.pull is not None:
            # Docker will take care of pulling when service is created
            continue
        if service.type == ServiceType.static and service.image.dockerfile is None:
            continue
        image = docker.image_name(
            registry_host=new_deployment_info.registry_host,
            project_name=new_deployment_info.project_name,
            deployment_number=new_deployment_info.number,
            dockerfile=service.image.dockerfile or "Dockerfile",
            context=service.image.context or ".",
        )
        if image not in images:
            images.add(image)
            log_output(f"Building image of {service_name}: {image}\n")
            docker.build_image(
                image=image,
                project_name=new_deployment_info.project_name,
                dockerfile=service.image.dockerfile or "Dockerfile",
                context=service.image.context or ".",
                log_output=log_output,
            )
    return list(images)


def push_images(
    images: list[str],
    log_output: Callable[[str], None],
) -> None:
    log_output("Pushing images to Disco registry\n")
    for image in images:
        docker.push_image(image, log_output=log_output)


def create_networks(
    new_deployment_info: DeploymentInfo,
    recovery: bool,
    log_output: Callable[[str], None],
) -> None:
    try:
        network_name = docker.deployment_network_name(
            new_deployment_info.project_name, new_deployment_info.number
        )
        # TODO when in recovery, we should check if the network exists before
        #      trying to create it.
        docker.create_network(
            network_name,
            log_output,
        )
    except Exception:
        if recovery:
            log_output(f"Failed to create network {network_name}\n")
        else:
            raise

    assert new_deployment_info.disco_file is not None
    if "web" in new_deployment_info.disco_file.services:
        web_network = docker.deployment_web_network_name(
            new_deployment_info.project_name, new_deployment_info.number
        )
        try:
            # TODO when in recovery, we should check if the network exists before
            #      trying to create it.
            docker.create_network(web_network, log_output)
        except Exception:
            if recovery:
                log_output(f"Failed to create network {web_network}\n")
            else:
                raise
        try:
            # TODO when in recovery, we should check if the network is
            #      already connected to the container
            docker.add_network_to_container("disco-caddy", web_network, log_output)
        except Exception:
            if recovery:
                log_output(f"Failed to add network {web_network} to disco-caddy\n")
            else:
                raise


def start_services(
    new_deployment_info: DeploymentInfo,
    recovery: bool,
    log_output: Callable[[str], None],
) -> None:
    log_output("Starting services\n")
    assert new_deployment_info.disco_file is not None
    for service_name, service in new_deployment_info.disco_file.services.items():
        if service.type != ServiceType.container:
            continue
        networks = [
            docker.deployment_network_name(
                new_deployment_info.project_name, new_deployment_info.number
            )
        ]
        if service_name == "web":
            networks.append(
                docker.deployment_web_network_name(
                    new_deployment_info.project_name, new_deployment_info.number
                )
            )
        internal_service_name = docker.service_name(
            new_deployment_info.project_name, service_name, new_deployment_info.number
        )
        if service.image.pull is not None:
            image = service.image.pull
        else:
            image = docker.image_name(
                registry_host=new_deployment_info.registry_host,
                project_name=new_deployment_info.project_name,
                deployment_number=new_deployment_info.number,
                dockerfile=service.image.dockerfile or "Dockerfile",
                context=service.image.context or ".",
            )
        log_output(f"Starting service {service_name}\n")
        try:
            # TODO in recovery, we should check if service is already running first
            docker.start_service(
                image=image,
                name=internal_service_name,
                project_name=new_deployment_info.project_name,
                project_service_name=service_name,
                deployment_number=new_deployment_info.number,
                env_variables=new_deployment_info.env_variables,
                volumes=[(v.name, v.destination_path) for v in service.volumes],
                published_ports=[
                    (p.published_as, p.from_container_port, p.protocol)
                    for p in service.published_ports
                ],
                networks=networks,
                command=service.command,
                log_output=log_output,
            )
        except Exception:
            if recovery:
                log_output(f"Failed to start service {service_name}\n")
            else:
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
            f"Stopping previous service {service_name} "
            f"(published port would conflict with new service)\n"
        )
        try:
            # TODO in recovery, we should check if service is already stopped first
            docker.stop_service(internal_service_name, log_output=log_output)
        except Exception:
            if recovery:
                log_output(f"Failed to start service {service_name}\n")
            else:
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
        log_output("Sending traffic to new web service\n")
        assert new_deployment_info.disco_file is not None
        try:
            caddy.serve_service(
                new_deployment_info.project_name,
                internal_service_name,
                port=new_deployment_info.disco_file.services["web"].port or 8000,
            )
        except Exception:
            if recovery:
                log_output(
                    f"Failed to update Caddy to serve "
                    f"deployment {new_deployment_info.number}\n"
                )
            else:
                raise
    else:  # static
        try:
            caddy.serve_static_site(
                new_deployment_info.project_name, new_deployment_info.number
            )
        except Exception:
            if recovery:
                log_output(
                    f"Failed to update Caddy to serve "
                    f"deployment {new_deployment_info.number}\n"
                )
            else:
                raise


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
        if recovery:
            log_output("Failed to retrieve list of services to stop\n")
            return
        else:
            raise

    for service in all_services - current_services:
        try:
            docker.stop_service(service, log_output)
        except Exception:
            if recovery:
                log_output(f"Failed to stop service {service}\n")
            else:
                raise


def remove_prev_networks(
    prev_deployment_info: DeploymentInfo | None,
    recovery: bool,
    log_output: Callable[[str], None],
) -> None:
    if prev_deployment_info is None:
        return
    try:
        network_name = docker.deployment_network_name(
            prev_deployment_info.project_name, prev_deployment_info.number
        )
        # TODO when in recovery, check that network exists first
        docker.remove_network(
            network_name,
            log_output,
        )
    except Exception:
        if recovery:
            log_output(f"Failed to remove network {network_name}\n")
        else:
            raise
    assert prev_deployment_info.disco_file is not None
    if (
        "web" in prev_deployment_info.disco_file.services
        and prev_deployment_info.disco_file.services["web"].type
        == ServiceType.container
    ):
        web_network = docker.deployment_web_network_name(
            prev_deployment_info.project_name, prev_deployment_info.number
        )
        try:
            docker.remove_network_from_container("disco-caddy", web_network, log_output)
        except Exception:
            if recovery:
                log_output(f"Failed to remove network {web_network} from disco-caddy\n")
            else:
                raise
        try:
            docker.remove_network(web_network, log_output)
        except Exception:
            if recovery:
                log_output(f"Failed to remove network {web_network}\n")
            else:
                raise


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
    log_output("Copying static files\n")
    copy_static_site_src_to_deployment_folder(
        project_name=new_deployment_info.project_name,
        public_path=new_deployment_info.disco_file.services["web"].public_path,
        deployment_number=new_deployment_info.number,
    )
