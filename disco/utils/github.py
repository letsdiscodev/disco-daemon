import json
import logging
import re
import shutil
import subprocess

from disco.utils.filesystem import project_path, projects_root

log = logging.getLogger(__name__)


def fetch(project_name: str) -> None:
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
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Git returned status {process.returncode}")


def clone_project(
    project_name: str,
    github_repo: str,
    github_host: str,
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
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Git returned status {process.returncode}")


def checkout_commit(project_name: str, commit_hash: str) -> None:
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
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Git returned status {process.returncode}")


def checkout_latest(project_name: str) -> None:
    log.info("Checking out latest commit from Github project %s", project_name)
    branch = main_or_master(project_name)  # TODO receive branch as arg
    args = ["git", "checkout", f"origin/{branch}"]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=project_path(project_name),
    )
    assert process.stdout is not None
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Git returned status {process.returncode}")


def get_head_commit_hash(project_name: str) -> str:
    log.info("Getting head commit hash for %s", project_name)
    args = ["git", "rev-parse", "HEAD"]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=project_path(project_name),
    )
    assert process.stdout is not None
    for line in process.stdout:
        decoded_line = line.decode("utf-8")
        hash = decoded_line.replace("\n", "")

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Git returned status {process.returncode}")

    if not re.match(r"^[a-f0-9]{40}$", hash):
        raise Exception(f"Invalid commit hash returned by 'git rev-parse HEAD': {hash}")
    return hash


def main_or_master(project_name: str) -> str:
    log.info("Finding if origin/master or origin/main exists in %s", project_name)
    args = [
        "git",
        "branch",
        "--remote",
        "-l",
        "origin/master",
        "origin/main",
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=project_path(project_name),
    )
    assert process.stdout is not None
    main_exists = False
    master_exists = False
    for line in process.stdout:
        if "origin/main" in line.decode("utf-8"):
            main_exists = True
        if "origin/master" in line.decode("utf-8"):
            master_exists = True
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Git returned status {process.returncode}")
    if master_exists:
        return "master"
    if main_exists:
        return "main"
    raise Exception(f"No 'main' or 'master' branch found for {project_name}")


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
    log.info("Removing Github repo %s", project_name)
    shutil.rmtree(project_path(project_name))


def repo_is_public(github_repo: str) -> bool:
    log.info("Checking if Github repo is accessible %s", github_repo)
    args = ["git", "ls-remote", github_repo]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for _ in process.stdout:
        pass  # no op, just swallow output
    process.wait()
    return process.returncode == 0
