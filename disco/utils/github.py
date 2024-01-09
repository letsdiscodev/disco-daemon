import json
import logging
import os
import re
import subprocess

log = logging.getLogger(__name__)


def pull(project_name: str, github_repo: str, github_host: str) -> None:
    args = ["git", "pull"]
    directory = f"/code/projects/{project_name}"
    if not os.path.isdir(directory):
        _clone_project(project_name, github_repo, github_host)
    else:
        try:
            subprocess.run(
                args=args,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=f"/code/projects/{project_name}",
            )
        except subprocess.CalledProcessError as ex:
            raise Exception(ex.stdout.decode("utf-8")) from ex


def _clone_project(project_name: str, github_repo: str, github_host: str) -> None:
    url = github_repo.replace("github.com", github_host)
    args = ["git", "clone", url, f"/code/projects/{project_name}"]
    try:
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as ex:
        raise Exception(ex.stdout.decode("utf-8")) from ex


def get_branch_for_webhook_push(request_body):
    body = json.loads(request_body)
    refs = body["ref"]
    branch = _branch_from_refs(refs)
    return branch


def _branch_from_refs(refs):
    """Receives a string like 'refs/heads/master' and returns 'master'."""
    match = re.match(r"refs/heads/(?P<branch>.+)", refs)
    return match.group("branch")
