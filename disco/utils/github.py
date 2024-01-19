import json
import logging
import re
import subprocess

from disco.utils.filesystem import project_path, projects_root

log = logging.getLogger(__name__)


def fetch(project_id: str) -> None:
    log.info("Pulling from Github project %s", project_id)
    args = ["git", "fetch", "origin"]
    try:
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=project_path(project_id),
        )
    except subprocess.CalledProcessError as ex:
        raise Exception(ex.stdout.decode("utf-8")) from ex


def clone_project(project_id: str, github_repo: str, github_host: str) -> None:
    log.info("Cloning from Github project %s (%s)", project_id, github_repo)
    url = github_repo.replace("github.com", github_host)
    args = ["git", "clone", url, project_path(project_id)]
    try:
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=projects_root(),
        )
    except subprocess.CalledProcessError as ex:
        raise Exception(ex.stdout.decode("utf-8")) from ex


def checkout_commit(project_id: str, commit_hash: str) -> None:
    log.info("Checking out commit from Github project %s: %s", project_id, commit_hash)
    args = ["git", "checkout", commit_hash]
    try:
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=project_path(project_id),
        )
    except subprocess.CalledProcessError as ex:
        raise Exception(ex.stdout.decode("utf-8")) from ex


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
