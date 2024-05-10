import json
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import requests
from jwt import JWT, jwk_from_pem

from disco.models.db import Session
from disco.utils.filesystem import project_path, projects_root
from disco.utils.githubapps import (
    get_github_app_installation_by_id,
    get_repo_by_id_sync,
    get_repos_by_full_name_sync,
)

log = logging.getLogger(__name__)


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


def fetch(project_name: str, repo_full_name: str) -> None:
    log.info("Fetching from Github project %s", project_name)
    access_token = get_access_token_for_github_app_repo(full_name=repo_full_name)
    if access_token is not None:
        log.info("Using access token to fetch repo %s", repo_full_name)
        url = f"https://x-access-token:{access_token}@github.com/{repo_full_name}"
    else:
        log.info("Not using access token to fetch repo %s", repo_full_name)
        url = f"https://github.com/{repo_full_name}"
    args = ["git", "remote", "set-url", "origin", url]
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
        raise GithubException(f"Git returned status {process.returncode}")


def clone(
    project_name: str,
    repo_full_name: str,
) -> None:
    log.info("Cloning from Github project %s (%s)", project_name, repo_full_name)
    access_token = get_access_token_for_github_app_repo(full_name=repo_full_name)
    if access_token is not None:
        log.info("Using access token to clone repo %s", repo_full_name)
        url = f"https://x-access-token:{access_token}@github.com/{repo_full_name}"
    else:
        log.info("Not using access token to clone repo %s", repo_full_name)
        url = f"https://github.com/{repo_full_name}"
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
        raise GithubException(f"Git returned status {process.returncode}")


class GithubException(Exception):
    pass


def get_access_token_for_github_app_repo(full_name: str) -> str | None:
    with Session.begin() as dbsession:
        repos = get_repos_by_full_name_sync(dbsession, full_name)
        if len(repos) == 0:
            log.info("No Github app for repo %s, using anonymous access", full_name)
            return None
        repo_ids = [repo.id for repo in repos]
    for repo_id in repo_ids:
        with Session.begin() as dbsession:
            repo = get_repo_by_id_sync(dbsession, repo_id)
            if repo is None:  # in case it's been removed since fetching above
                continue
            access_token = repo.installation.access_token
            access_token_expires = repo.installation.access_token_expires
            installation_id = repo.installation.id
            pem = repo.installation.github_app.pem
            app_id = repo.installation.github_app.id
        if access_token_expires is None or access_token_expires < datetime.now(
            timezone.utc
        ) - timedelta(minutes=10):
            # renew access token
            signing_key = jwk_from_pem(pem.encode("utf-8"))
            payload = {
                "iat": int(time.time()),
                "exp": int(time.time()) + 30,
                "iss": app_id,
            }
            jwt_instance = JWT()
            encoded_jwt = jwt_instance.encode(payload, signing_key, alg="RS256")
            url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {encoded_jwt}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            response = requests.post(url=url, headers=headers, timeout=20)
            if response.status_code != 201:
                log.info(
                    "Github returned status code %d when creating "
                    "access token for %s with installation %s",
                    response.status_code,
                    full_name,
                    installation_id,
                )
                continue
            resp_body = response.json()
            access_token = resp_body["token"]
            assert isinstance(access_token, str)
            expires_iso8601 = resp_body["expires_at"]
            expires = datetime.fromisoformat(expires_iso8601)
            with Session.begin() as dbsession:
                installation = get_github_app_installation_by_id(
                    dbsession, installation_id
                )
                assert installation is not None
                installation.access_token = access_token
                installation.access_token_expires = expires
        assert access_token is not None
        # we have a token, make sure it still has access to the repo
        url = "https://api.github.com/installation/repositories"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        response = requests.get(url=url, headers=headers, timeout=20)
        if response.status_code != 200:
            log.info(
                "Github returned status code %d when fetching installation %d's repos, can't use for repo %s",
                response.status_code,
                installation_id,
                full_name,
            )
            continue
        resp_body = response.json()
        repo_full_names = [repo["full_name"] for repo in resp_body["repositories"]]
        if full_name not in repo_full_names:
            log.info(
                "Repository %s not accessible with app installation %d",
                full_name,
                installation_id,
            )
            continue
        return access_token
    log.info(
        "All Github apps were unable to provide an access token for %s, using anonymous access",
        full_name,
    )
    return None
