import subprocess

SSH_PATH = "/root/.ssh"  # from host, mounted with Docker

# TODO use project ID instead of name,
#      because name can change


def create_deploy_key(name: str) -> tuple[str, str]:
    _create_ssh_key(_path(name))
    _add_key_alias(name)
    return _github_host(name), _get_key_pub(name)


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
    with open(f"{SSH_PATH}/config", "a") as f:
        f.writelines(
            [
                "\n\n",
                f"Host {_github_host(name)}\n",
                "    Hostname github.com\n",
                f"    IdentityFile=/root/.ssh/{name}-deploy-key\n",
                "    StrictHostKeyChecking accept-new\n",
            ]
        )
