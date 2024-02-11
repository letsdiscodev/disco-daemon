import json
import logging
import re
import shutil
import subprocess
from typing import Callable

from disco.utils.filesystem import project_path, projects_root

log = logging.getLogger(__name__)


def fetch(project_name: str, log_output: Callable[[str], None]) -> None:
    log.info("Pulling from Github project %s", project_name)
    args = ["git", "fetch", "origin"]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=project_path(project_name),
    )
    assert process.stdout is not None
    for line in process.stdout:
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Git returned status {process.returncode}")


def clone_project(
    project_name: str,
    github_repo: str,
    github_host: str,
    log_output: Callable[[str], None],
) -> None:
    log.info("Cloning from Github project %s (%s)", project_name, github_repo)
    url = github_repo.replace("github.com", github_host)
    args = ["git", "clone", url, project_path(project_name)]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=projects_root(),
    )
    assert process.stdout is not None
    for line in process.stdout:
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Git returned status {process.returncode}")


def checkout_commit(
    project_name: str, commit_hash: str, log_output: Callable[[str], None]
) -> None:
    log.info(
        "Checking out commit from Github project %s: %s", project_name, commit_hash
    )
    args = ["git", "checkout", commit_hash]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=project_path(project_name),
    )
    assert process.stdout is not None
    for line in process.stdout:
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Git returned status {process.returncode}")


def get_commit_info_from_webhook_push(request_body: str) -> tuple[str, str]:
    body = json.loads(request_body)
    refs = body["ref"]
    branch = _branch_from_refs(refs)
    commit_hash = body["after"]
    return branch, commit_hash


def _branch_from_refs(refs: str) -> str:
    """Receives a string like 'refs/heads/master' and returns 'master'."""
    match = re.match(r"refs/heads/(?P<branch>.+)", refs)
    if match is None:
        raise Exception(f"Couldn't find branch name in refs {refs}")
    return match.group("branch")


def remove_repo(project_name: str) -> None:
    shutil.rmtree(project_path(project_name))
