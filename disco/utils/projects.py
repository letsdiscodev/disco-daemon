import logging
import uuid
from secrets import token_hex

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, Project
from disco.utils import caddy, docker, github, sshkeys
from disco.utils.caddy import add_project_route
from disco.utils.commandoutputs import delete_output_for_source
from disco.utils.envvariables import set_env_variables
from disco.utils.filesystem import remove_project_static_deployments_if_any
from disco.utils.sshkeys import create_deploy_key, set_deploy_key
from disco.utils.sshkeys import github_host as ssh_github_host

log = logging.getLogger(__name__)


def create_project(
    dbsession: DBSession,
    name: str,
    github_repo: str | None,
    github_webhook_token: str | None,
    github_webhook_secret: str | None,
    domain: str | None,
    ssh_key_pub: str | None,
    ssh_key_private: str | None,
    env_variables: list[tuple[str, str]],
    by_api_key: ApiKey,
) -> tuple[Project, str | None]:
    project = Project(
        id=uuid.uuid4().hex,
        name=name,
        github_repo=github_repo,
        domain=domain,
    )
    if ssh_key_pub is not None and ssh_key_private is not None:
        set_deploy_key(
            project_name=name, private_key=ssh_key_private, public_key=ssh_key_pub
        )
    if github_repo is not None:
        if github.repo_is_public(github_repo):
            project.github_host = "github.com"
            ssh_key_pub = None
        else:
            if github_webhook_token is None:
                github_webhook_token = token_hex(16)
            if github_webhook_secret is None:
                github_webhook_secret = token_hex(16)
            if ssh_key_pub is None:
                ssh_key_pub = create_deploy_key(name)
            github_host = ssh_github_host(name)
            project.github_host = github_host
            project.github_webhook_token = github_webhook_token
            project.github_webhook_secret = github_webhook_secret
    else:
        ssh_key_pub = None
    dbsession.add(project)
    set_env_variables(
        dbsession=dbsession,
        project=project,
        env_variables=env_variables,
        by_api_key=by_api_key,
    )
    if domain is not None:
        add_project_route(project_name=project.name, domain=project.domain)
    log.info("%s created project %s", by_api_key.log(), project.log())
    return project, ssh_key_pub


def get_project_by_id(dbsession: DBSession, project_id: str) -> Project | None:
    return dbsession.query(Project).get(project_id)


def get_project_by_name(dbsession: DBSession, name: str) -> Project | None:
    return dbsession.query(Project).filter(Project.name == name).first()


def get_project_by_domain(dbsession: DBSession, domain: str) -> Project | None:
    return dbsession.query(Project).filter(Project.domain == domain).first()


def get_project_by_github_webhook_token(
    dbsession: DBSession, webhook_token: str
) -> Project | None:
    return (
        dbsession.query(Project)
        .filter(Project.github_webhook_token == webhook_token)
        .first()
    )


def get_all_projects(dbsession: DBSession) -> list[Project]:
    return dbsession.query(Project).all()


def delete_project(dbsession: DBSession, project: Project, by_api_key: ApiKey) -> None:
    log.info("%s is deleting project %s", by_api_key.log(), project.log())
    if project.github_repo is not None:
        try:
            sshkeys.remove_deploy_key(project.name)
        except Exception:
            log.info("Failed to remove SSH deploy key for project %s", project.name)
        try:
            github.remove_repo(project.name)
        except Exception:
            log.info("Failed to remove Github repo for project %s", project.name)
    remove_project_static_deployments_if_any(project.name)
    try:
        caddy.remove_project_route(project.name)
    except Exception:
        log.info("Failed to remove reverse proxy route for project %s", project.name)
    services = docker.list_services_for_project(project.name)
    for service_name in services:
        try:
            docker.stop_service(service_name)
        except Exception:
            log.info("Failed to stop service %s", service_name)
    containers = docker.list_containers_for_project(project.name)
    for container in containers:
        docker.remove_container(container)
    networks = docker.list_networks_for_project(project.name)
    for network in networks:
        try:
            docker.remove_network_from_container("disco-caddy", network)
        except Exception:
            pass
        try:
            docker.remove_network(network)
        except Exception:
            log.info("Failed to remove network %s", network)
    for env_var in project.env_variables:
        dbsession.delete(env_var)
    for deployment in project.deployments:
        delete_output_for_source(dbsession, f"DEPLOYMENT_{deployment.id}")
        for env_var in deployment.env_variables:
            dbsession.delete(env_var)
        dbsession.delete(deployment)
    for keyvalue in project.key_values:
        dbsession.delete(keyvalue)
    for run in project.command_runs:
        dbsession.delete(run)
    dbsession.delete(project)
