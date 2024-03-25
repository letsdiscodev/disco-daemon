import logging
import os
import shutil

log = logging.getLogger(__name__)


def projects_root() -> str:
    return "/disco/projects"


def project_path(project_name: str) -> str:
    return f"/disco/projects/{project_name}"


def project_path_on_host(host_home: str, project_name: str) -> str:
    return f"{host_home}{project_path(project_name)}"


def project_folder_exists(project_name: str):
    return os.path.isdir(project_path(project_name))


def read_disco_file(project_name: str) -> str | None:
    path = f"{project_path(project_name)}/disco.json"
    log.info("Reading disco file %s", path)
    if not os.path.isfile(path):
        log.info("Disco file does not exist, not reading %s", path)
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


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


def create_static_site_deployment_directory(
    host_home: str, project_name: str, deployment_number: int
) -> str:
    path = static_site_deployment_path(project_name, deployment_number)
    os.makedirs(path)
    return static_site_deployment_path_host_machine(
        host_home, project_name, deployment_number
    )


def remove_project_static_deployments_if_any(project_name: str) -> None:
    path = static_site_deployments_path(project_name)
    if os.path.isdir(path):
        shutil.rmtree(path)


def static_site_src_public_path(project_name: str, public_path: str) -> str:
    path = os.path.abspath(f"{project_path(project_name)}{public_path}")
    if not path.startswith(f"{project_path(project_name)}/"):
        # prevent traversal attacks
        raise Exception("publicPath must be inside project folder")
    return path


def copy_static_site_src_to_deployment_folder(
    project_name: str, public_path: str, deployment_number: int
) -> None:
    src_path = static_site_src_public_path(project_name, public_path)
    dst_path = static_site_deployment_path(project_name, deployment_number)
    shutil.copytree(src_path, dst_path)


def _certificate_directory(domain: str) -> str:
    return f"/disco/caddy/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/{domain}"


def get_caddy_key_crt(domain: str) -> str:
    path = f"{_certificate_directory(domain)}/{domain}.crt"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def set_caddy_key_crt(domain: str, value: str) -> None:
    directory = _certificate_directory(domain)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    path = f"{directory}/{domain}.crt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)


def get_caddy_key_key(domain: str) -> str:
    path = f"{_certificate_directory(domain)}/{domain}.key"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def set_caddy_key_key(domain: str, value: str) -> None:
    directory = _certificate_directory(domain)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    path = f"{directory}/{domain}.key"
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)


def get_caddy_key_meta(domain: str) -> str:
    path = f"{_certificate_directory(domain)}/{domain}.json"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def set_caddy_key_meta(domain: str, value: str) -> None:
    directory = _certificate_directory(domain)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    path = f"{directory}/{domain}.json"
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
