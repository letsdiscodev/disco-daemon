import logging
import os
import subprocess

log = logging.getLogger(__name__)

SSH_PATH = "/root/.ssh"  # from host, mounted with Docker


def create_deploy_key(project_name: str) -> str:
    log.info("Creating SSH deployment keys for project %s", project_name)
    _create_ssh_key(_path(project_name))
    _add_key_alias(project_name)
    key_pub = get_key_pub(project_name)
    assert key_pub is not None
    return key_pub


def set_deploy_key(project_name: str, private_key: str, public_key: str) -> None:
    log.info("Setting SSH deployment keys for project %s", project_name)
    set_key_pub(project_name, public_key)
    set_key_private(project_name, private_key)
    _add_key_alias(project_name)


def remove_deploy_key(project_name: str) -> None:
    log.info("Removing SSH deployment keys for project %s", project_name)
    os.remove(_path(project_name))
    os.remove(f"{_path(project_name)}.pub")
    _remove_key_alias(project_name)


def _path(project_name: str) -> str:
    return f"{SSH_PATH}/{project_name}-deploy-key"


def github_host(project_name: str) -> str:
    return f"github.com-{project_name}"


def _create_ssh_key(path: str) -> None:
    args = [
        "ssh-keygen",
        "-f",  # file path
        path,
        "-q",  # not interactive
        "-N",  # new password
        "",  # empty password
    ]
    try:
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as ex:
        raise Exception(ex.stdout.decode("utf-8")) from ex


def get_key_pub(project_name: str) -> str | None:
    path = f"{_path(project_name)}.pub"
    if not os.path.isfile(f"{_path(project_name)}.pub"):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def set_key_pub(project_name: str, value: str) -> None:
    path = f"{_path(project_name)}.pub"
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
    os.chmod(path, 0o600)


def get_key_private(project_name: str) -> str | None:
    path = f"{_path(project_name)}"
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def set_key_private(project_name: str, value: str) -> None:
    path = f"{_path(project_name)}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
    os.chmod(path, 0o600)


def _add_key_alias(project_name: str) -> None:
    with open(f"{SSH_PATH}/config", "a", encoding="utf-8") as f:
        f.writelines(_key_alias(project_name))


def _remove_key_alias(project_name) -> None:
    with open(f"{SSH_PATH}/config", "r", encoding="utf-8") as f:
        ssh_config = f.read()
    new_ssh_config = ssh_config.replace("".join(_key_alias(project_name)), "")
    with open(f"{SSH_PATH}/config", "w", encoding="utf-8") as f:
        f.write(new_ssh_config)


def _key_alias(project_name) -> list[str]:
    return [
        "\n\n",
        f"Host {github_host(project_name)}\n",
        "    Hostname github.com\n",
        f"    IdentityFile=/root/.ssh/{project_name}-deploy-key\n",
        "    StrictHostKeyChecking accept-new\n",
    ]
