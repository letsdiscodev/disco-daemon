import os
import subprocess

SSH_PATH = "/root/.ssh"  # from host, mounted with Docker


def create_deploy_key(name: str) -> tuple[str, str]:
    _create_ssh_key(_path(name))
    _add_key_alias(name)
    return _github_host(name), _get_key_pub(name)


def remove_deploy_key(name: str) -> None:
    os.remove(_path(name))
    os.remove(f"{_path(name)}.pub")
    _remove_key_alias(name)


def _path(name: str) -> str:
    return f"{SSH_PATH}/{name}-deploy-key"


def _github_host(name: str) -> str:
    return f"github.com-{name}"


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


def _get_key_pub(name: str) -> str:
    with open(f"{_path(name)}.pub", "r", encoding="utf-8") as f:
        return f.read()


def _add_key_alias(name: str) -> None:
    with open(f"{SSH_PATH}/config", "a", encoding="utf-8") as f:
        f.writelines(_key_alias(name))


def _remove_key_alias(name) -> None:
    with open(f"{SSH_PATH}/config", "r", encoding="utf-8") as f:
        ssh_config = f.read()
    new_ssh_config = ssh_config.replace("".join(_key_alias(name)), "")
    with open(f"{SSH_PATH}/config", "w", encoding="utf-8") as f:
        f.write(new_ssh_config)


def _key_alias(name) -> list[str]:
    return [
        "\n\n",
        f"Host {_github_host(name)}\n",
        "    Hostname github.com\n",
        f"    IdentityFile=/root/.ssh/{name}-deploy-key\n",
        "    StrictHostKeyChecking accept-new\n",
    ]
