import asyncio
import hashlib
import hmac
import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import Literal, Sequence

import requests
from jwt import JWT, jwk_from_pem
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import (
    ApiKey,
    GithubApp,
    GithubAppInstallation,
    GithubAppRepo,
    PendingGithubApp,
)
from disco.models.db import AsyncSession, Session
from disco.utils.filesystem import project_path, projects_root

log = logging.getLogger(__name__)


class GithubException(Exception):
    pass


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
    branch = main_or_master(project_name)
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
    log.info("Removing Github repo of project %s from filesystem", project_name)
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


def get_access_token_for_github_app_repo(full_name: str) -> str | None:
    log.info("Getting Github access token for repo %s", full_name)
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
            installation_id = repo.installation.id
        try:
            log.info(
                "Using Github installation %d for repo %s", installation_id, full_name
            )
            access_token = asyncio.run(
                get_access_token_for_installation_id(installation_id)
            )
        except GithubException as ex:
            log.info(
                "Failed to obtain Github access token for installation %d for repo %s: %s",
                installation_id,
                full_name,
                ex,
            )
            continue
        assert access_token is not None
        try:
            repo_full_names = asyncio.run(
                fetch_repository_list_for_github_app_installation(access_token)
            )
        except GithubException as ex:
            log.info(
                "Failed to obtain Github repositories for installation %d: %s",
                installation_id,
                ex,
            )
            continue
        if full_name not in repo_full_names:
            log.info(
                "Repository %s not accessible with app installation %d",
                full_name,
                installation_id,
            )
            continue
        return access_token
    log.info(
        "All Github apps were unable to provide an access token "
        "for %s, using anonymous access",
        full_name,
    )
    return None


def generate_jwt_token(app_id: int, pem: str) -> str:
    signing_key = jwk_from_pem(pem.encode("utf-8"))
    payload = {
        "iat": int(time.time()),
        "exp": int(time.time()) + 30,
        "iss": app_id,
    }
    jwt_instance = JWT()
    return jwt_instance.encode(payload, signing_key, alg="RS256")


def create_pending_github_app(
    dbsession: DBSession, organization: str | None, by_api_key: ApiKey
) -> PendingGithubApp:
    pending_app = PendingGithubApp(
        id=uuid.uuid4().hex,
        state=token_hex(16),
        expires=datetime.now(timezone.utc) + timedelta(minutes=30),
        organization=organization,
    )
    dbsession.add(pending_app)
    log.info("%s created pending Github app %s", by_api_key.log(), pending_app.log())
    return pending_app


def generate_new_pending_app_state(pending_app: PendingGithubApp) -> None:
    log.info(
        "Generating new Github pending app state for pending app %s", pending_app.id
    )
    pending_app.state = token_hex(16)


def get_github_pending_app_by_id(
    dbsession: DBSession, pending_app_id: str
) -> PendingGithubApp | None:
    return dbsession.get(PendingGithubApp, pending_app_id)


def delete_pending_github_app(dbsession, pending_app: PendingGithubApp) -> None:
    log.info("Deleting Github pending app %s", pending_app.id)
    dbsession.delete(pending_app)


def create_github_app(
    dbsession: DBSession,
    app_id: int,
    slug: str,
    name: str,
    owner_id: int,
    owner_login: str,
    owner_type: Literal["User", "Organization"],
    webhook_secret: str,
    pem: str,
    client_secret: str,
    html_url: str,
    app_info: str,
) -> GithubApp:
    log.info("Creating Github app %d for %s (%s)", app_id, owner_login, owner_type)
    github_app = GithubApp(
        id=app_id,
        slug=slug,
        name=name,
        owner_id=owner_id,
        owner_login=owner_login,
        owner_type=owner_type,
        webhook_secret=webhook_secret,
        pem=pem,
        client_secret=client_secret,
        html_url=html_url,
        app_info=app_info,
    )
    dbsession.add(github_app)
    return github_app


async def get_all_github_apps(dbsession: AsyncDBSession) -> Sequence[GithubApp]:
    stmt = select(GithubApp).order_by(GithubApp.owner_login)
    result = await dbsession.execute(stmt)
    return result.scalars().all()


def get_github_app_by_id_sync(dbsession: DBSession, app_id: int) -> GithubApp | None:
    return dbsession.query(GithubApp).get(app_id)


async def get_github_app_by_id(
    dbsession: AsyncDBSession, app_id: int
) -> GithubApp | None:
    return await dbsession.get(GithubApp, app_id)


def process_github_app_webhook(
    request_body_bytes: bytes,
    x_github_event: str | None,
    x_hub_signature_256: str | None,
    x_github_hook_installation_target_type: str | None,
    x_github_hook_installation_target_id: str | None,
) -> None:
    body_text = request_body_bytes.decode("utf-8")
    body = json.loads(body_text)
    log.info("Processing Github app webhook '%s': %s", x_github_event, body)

    if x_github_event is None:
        log.warning("X-GitHub-Event not provided, skipping")
        return
    if x_hub_signature_256 is None:
        log.warning("X-Hub-Signature-256 not provided, skipping")
        return
    if x_github_hook_installation_target_type is None:
        log.warning("X-GitHub-Hook-Installation-Target-Type not provided, skipping")
        return
    if x_github_hook_installation_target_type != "integration":
        log.warning(
            "X-GitHub-Hook-Installation-Target-Type not 'integration', skipping"
        )
        return
    if x_github_hook_installation_target_id is None:
        log.warning("X-GitHub-Hook-Installation-Target-ID not provided, skipping")
        return

    with Session.begin() as dbsession:
        github_app = get_github_app_by_id_sync(
            dbsession, int(x_github_hook_installation_target_id)
        )
        if github_app is None:
            log.warning(
                "X-GitHub-Hook-Installation-Target-ID did not match existing app, skipping"
            )
            return
        webhook_secret = github_app.webhook_secret

    hash_object = hmac.new(
        webhook_secret.encode("utf-8"),
        msg=request_body_bytes,
        digestmod=hashlib.sha256,
    )
    expected_signature = "sha256=" + hash_object.hexdigest()
    if not hmac.compare_digest(expected_signature, x_hub_signature_256):
        log.warning("X-Hub-Signature-256 does not match, skipping")
        return
    log.info("X-Hub-Signature-256 signature matched, continuing")
    log.info("Github event: %s", x_github_event)
    if x_github_event == "push":
        from disco.utils.deployments import create_deployment_sync
        from disco.utils.github import get_commit_info_from_webhook_push
        from disco.utils.mq.tasks import enqueue_task_deprecated
        from disco.utils.projects import get_projects_by_github_app_repo

        try:
            full_name = body["repository"]["full_name"]
            branch, commit_hash = get_commit_info_from_webhook_push(body_text)
            if branch not in ["master", "main"]:
                log.info("Branch was not master or main, skipping")
                return
            deployment_ids = []
            with Session.begin() as dbsession:
                projects = get_projects_by_github_app_repo(dbsession, full_name)
                for project in projects:
                    deployment = create_deployment_sync(
                        dbsession=dbsession,
                        project=project,
                        commit_hash=commit_hash,
                        disco_file=None,
                        by_api_key=None,
                    )
                    deployment_ids.append(deployment.id)
            for deployment_id in deployment_ids:
                enqueue_task_deprecated(
                    task_name="PROCESS_DEPLOYMENT",
                    body=dict(
                        deployment_id=deployment_id,
                    ),
                )
        except KeyError:
            log.info("Not able to extract key info from Github webhook, skipping")
            return
    elif x_github_event == "installation_repositories":
        # user added/removed repos from the app installation
        try:
            app_id = body["installation"]["app_id"]
            installation_id = body["installation"]["id"]
        except KeyError:
            log.info("Not able to extract key info from Github webhook, skipping")
            return
        with Session.begin() as dbsession:
            github_app = get_github_app_by_id_sync(dbsession, app_id)
            if github_app is None:
                log.warning("Couldn't find Github app %d", app_id)
                return
            installation = get_github_app_installation_by_id_sync(
                dbsession, installation_id
            )
            if installation is None:
                log.warning("Couldn't find Github app installation %d", installation_id)
                return
            for repo in body["repositories_added"]:
                add_repository_to_installation_sync(
                    dbsession, installation, repo["full_name"]
                )
            for repo in body["repositories_removed"]:
                remove_repository_from_installation_sync(
                    dbsession, installation, repo["full_name"]
                )
    elif x_github_event == "installation":
        # user installed/uninstalled app
        try:
            action = body["action"]
            app_id = body["installation"]["app_id"]
            installation_id = body["installation"]["id"]
        except KeyError:
            log.info("Not able to extract key info from Github webhook, skipping")
            return
        with Session.begin() as dbsession:
            github_app = get_github_app_by_id_sync(dbsession, app_id)
            if github_app is None:
                log.warning("Couldn't find Github app %d", app_id)
                return
            if action == "created":
                # app installed
                installation = create_github_app_installation(
                    dbsession, github_app, installation_id
                )
                for repo in body["repositories"]:
                    add_repository_to_installation_sync(
                        dbsession, installation, repo["full_name"]
                    )
            elif action == "deleted":
                # app uninstalled
                installation = get_github_app_installation_by_id_sync(
                    dbsession, installation_id
                )
                if installation is None:
                    log.warning(
                        "Couldn't find Github app installation %d", installation_id
                    )
                    return
                delete_github_app_installation_sync(dbsession, installation)
            else:
                log.warning(
                    "Github App webhook action not handled %s, skipping", action
                )
    else:
        log.warning("Github App webhook event not handled %s, skipping", x_github_event)


def create_github_app_installation(
    dbsession: DBSession, github_app: GithubApp, installation_id: int
) -> GithubAppInstallation:
    log.info("Creating Github app installation %d", installation_id)
    installation = dbsession.get(GithubAppInstallation, installation_id)
    if installation is not None:
        log.info("Github installation %d aleady existed", installation_id)
        return installation
    installation = GithubAppInstallation(
        id=installation_id,
        github_app=github_app,
    )
    dbsession.add(installation)
    return installation


def get_github_app_installation_by_id_sync(
    dbsession: DBSession, installation_id: int
) -> GithubAppInstallation | None:
    return dbsession.query(GithubAppInstallation).get(installation_id)


async def get_github_app_installation_by_id(
    dbsession: AsyncDBSession, installation_id: int
) -> GithubAppInstallation | None:
    return await dbsession.get(GithubAppInstallation, installation_id)


def add_repository_to_installation_sync(
    dbsession: DBSession, installation: GithubAppInstallation, repo_full_name: str
) -> GithubAppRepo:
    log.info(
        "Adding Github repo %s to installation %d", repo_full_name, installation.id
    )
    stmt = (
        select(GithubAppRepo)
        .where(GithubAppRepo.full_name == repo_full_name)
        .where(GithubAppRepo.installation == installation)
        .limit(1)
    )
    result = dbsession.execute(stmt)
    repo = result.scalars().first()
    if repo is not None:
        log.info(
            "Github repo %s aleady existed for installation %d",
            repo_full_name,
            installation.id,
        )
        return repo
    repo = GithubAppRepo(
        id=uuid.uuid4().hex,
        full_name=repo_full_name,
        installation=installation,
    )
    dbsession.add(repo)
    return repo


async def add_repository_to_installation(
    dbsession: AsyncDBSession, installation: GithubAppInstallation, repo_full_name: str
) -> GithubAppRepo:
    log.info(
        "Adding Github repo %s to installation %d", repo_full_name, installation.id
    )
    stmt = (
        select(GithubAppRepo)
        .where(GithubAppRepo.full_name == repo_full_name)
        .where(GithubAppRepo.installation == installation)
        .limit(1)
    )
    result = await dbsession.execute(stmt)
    repo = result.scalars().first()
    if repo is not None:
        log.info(
            "Github repo %s aleady existed for installation %d",
            repo_full_name,
            installation.id,
        )
        return repo
    repo = GithubAppRepo(
        id=uuid.uuid4().hex,
        full_name=repo_full_name,
        installation=installation,
    )
    dbsession.add(repo)
    return repo


def remove_repository_from_installation_sync(
    dbsession: DBSession, installation: GithubAppInstallation, repo_full_name: str
) -> None:
    log.info(
        "Removing Github repo %s from installation %d", repo_full_name, installation.id
    )
    dbsession.query(GithubAppRepo).filter(
        GithubAppRepo.installation == installation
    ).filter(GithubAppRepo.full_name == repo_full_name).delete()


async def remove_repository_from_installation(
    dbsession: AsyncDBSession, installation: GithubAppInstallation, repo_full_name: str
) -> None:
    log.info(
        "Removing Github repo %s from installation %d", repo_full_name, installation.id
    )
    stmt = (
        delete(GithubAppRepo)
        .where(GithubAppRepo.installation == installation)
        .where(GithubAppRepo.full_name == repo_full_name)
    )
    await dbsession.execute(stmt)


def delete_github_app_installation_sync(
    dbsession: DBSession, installation: GithubAppInstallation
) -> None:
    for github_repo in installation.github_app_repos:
        remove_repository_from_installation_sync(
            dbsession, installation, github_repo.full_name
        )
    log.info(
        "Deleting Github app installation %d of app %d %s (%s)",
        installation.id,
        installation.github_app_id,
        installation.github_app.owner_login,
        installation.github_app.owner_type,
    )
    dbsession.delete(installation)


async def delete_github_app_installation(
    dbsession: AsyncDBSession, installation: GithubAppInstallation
) -> None:
    github_repos = await installation.awaitable_attrs.github_app_repos
    for github_repo in github_repos:
        await remove_repository_from_installation(
            dbsession, installation, github_repo.full_name
        )
    github_app = await installation.awaitable_attrs.github_app
    log.info(
        "Deleting Github app installation %d of app %d %s (%s)",
        installation.id,
        installation.github_app_id,
        github_app.owner_login,
        github_app.owner_type,
    )
    await dbsession.delete(installation)


def get_all_repos_sync(dbsession: DBSession) -> list[GithubAppRepo]:
    return dbsession.query(GithubAppRepo).order_by(GithubAppRepo.full_name).all()


async def get_all_repos(dbsession: AsyncDBSession) -> Sequence[GithubAppRepo]:
    stmt = select(GithubAppRepo).order_by(GithubAppRepo.full_name)
    result = await dbsession.execute(stmt)
    return result.scalars().all()


def get_repo_by_full_name_sync(
    dbsession: DBSession, full_name: str
) -> GithubAppRepo | None:
    return (
        dbsession.query(GithubAppRepo)
        .filter(GithubAppRepo.full_name == full_name)
        .first()
    )


def get_repos_by_full_name_sync(
    dbsession: DBSession, full_name: str
) -> Sequence[GithubAppRepo]:
    stmt = (
        select(GithubAppRepo)
        .join(GithubAppInstallation)
        .where(GithubAppRepo.full_name == full_name)
        .order_by(desc(GithubAppInstallation.access_token_expires))
    )
    result = dbsession.execute(stmt)
    return result.scalars().all()


async def get_repo_by_full_name(
    dbsession: AsyncDBSession, full_name: str
) -> GithubAppRepo | None:
    stmt = select(GithubAppRepo).where(GithubAppRepo.full_name == full_name).limit(1)
    result = await dbsession.execute(stmt)
    return result.scalars().first()


def get_repo_by_id_sync(
    dbsession: DBSession, github_repo_id: str
) -> GithubAppRepo | None:
    return dbsession.get(GithubAppRepo, github_repo_id)


async def get_repo_by_id(
    dbsession: AsyncDBSession, github_repo_id: str
) -> GithubAppRepo | None:
    return await dbsession.get(GithubAppRepo, github_repo_id)


async def delete_github_app(dbsession: AsyncDBSession, app: GithubApp):
    for installation in await app.awaitable_attrs.installations:
        await delete_github_app_installation(dbsession, installation)
    log.info(
        "Deleting Github app %d of %s (%s)", app.id, app.owner_login, app.owner_type
    )
    await dbsession.delete(app)


async def prune() -> None:
    log.info("Pruning Github apps")
    async with AsyncSession.begin() as dbsession:
        apps = await get_all_github_apps(dbsession)
        app_ids = [app.id for app in apps]
    for app_id in app_ids:
        if await app_still_exists(app_id):
            async with AsyncSession.begin() as dbsession:
                app = await get_github_app_by_id(dbsession, app_id)
                assert app is not None
                installations = await app.awaitable_attrs.installations
                installation_ids = [installation.id for installation in installations]
            for installation_id in installation_ids:
                try:
                    access_token = await get_access_token_for_installation_id(
                        installation_id
                    )
                    github_full_names = (
                        await fetch_repository_list_for_github_app_installation(
                            access_token
                        )
                    )
                except GithubException:
                    async with AsyncSession.begin() as dbsession:
                        installation = await get_github_app_installation_by_id(
                            dbsession, installation_id
                        )
                        assert installation is not None
                        app = await installation.awaitable_attrs.github_app
                        assert app is not None
                        log.info(
                            "Github app installation %d for %s %s (app ID %d) "
                            "is not accessible anymore, removing",
                            installation.id,
                            app.owner_login,
                            app.owner_type,
                            app.id,
                        )
                        await delete_github_app_installation(dbsession, installation)
                    continue
                async with AsyncSession.begin() as dbsession:
                    installation = await get_github_app_installation_by_id(
                        dbsession, installation_id
                    )
                    assert installation is not None
                    local_full_names = [
                        repo.full_name
                        for repo in await installation.awaitable_attrs.github_app_repos
                    ]
                    for full_name in github_full_names:
                        if full_name not in local_full_names:
                            log.info(
                                "Repository %s from installation %d discovered, adding",
                                full_name,
                                installation_id,
                            )
                            await add_repository_to_installation(
                                dbsession, installation, full_name
                            )
                    for full_name in local_full_names:
                        if full_name not in github_full_names:
                            log.info(
                                "Repository %s from installation %d "
                                "not available anymore, removing",
                                full_name,
                                installation_id,
                            )
                            await remove_repository_from_installation(
                                dbsession, installation, full_name
                            )
        else:
            async with AsyncSession.begin() as dbsession:
                app = await get_github_app_by_id(dbsession, app_id)
                assert app is not None
                log.info(
                    "Github app %d %s (%s) doesn't exist anymore, "
                    "removing local references to it",
                    app.id,
                    app.owner_login,
                    app.owner_type,
                )
                await delete_github_app(dbsession, app)


async def app_still_exists(app_id: int) -> bool:
    log.info("Verifying with Github if app %d still exists", app_id)
    async with AsyncSession.begin() as dbsession:
        app = await get_github_app_by_id(dbsession, app_id)
        if app is None:
            return False
        pem = app.pem
    encoded_jwt = generate_jwt_token(app_id=app_id, pem=pem)
    url = "https://api.github.com/app"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {encoded_jwt}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def query() -> requests.Response:
        return requests.get(url=url, headers=headers, timeout=20)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    still_exists = response.status_code == 200
    if still_exists:
        log.info("Github app %d still exists on Github", app_id)
    else:
        log.info("Github app %d doesn't exist on Github", app_id)
    return still_exists


async def get_access_token_for_installation_id(installation_id: int) -> str:
    log.info("Getting Github access token for installation %d", installation_id)
    async with AsyncSession.begin() as dbsession:
        installation = await get_github_app_installation_by_id(
            dbsession, installation_id
        )
        if installation is None:
            raise GithubException(f"Installation {installation_id} not found locally")
        access_token = installation.access_token
        expires = installation.access_token_expires
        app = await installation.awaitable_attrs.github_app
        app_id = app.id
        pem = app.pem
    if (
        access_token is not None
        and expires is not None
        and expires > datetime.now(timezone.utc) + timedelta(minutes=5)
    ):
        log.info(
            "Cached Github access token for installation %d "
            "still valid, using cached value",
            installation_id,
        )
        return access_token
    access_token, expires = await fetch_access_token(
        app_id=app_id, installation_id=installation_id, pem=pem
    )
    async with AsyncSession.begin() as dbsession:
        installation = await get_github_app_installation_by_id(
            dbsession, installation_id
        )
        if installation is None:
            raise GithubException(f"Installation {installation_id} not found locally")
        installation.access_token = access_token
        installation.access_token_expires = expires
    return access_token


async def fetch_access_token(
    app_id: int, installation_id: int, pem: str
) -> tuple[str, datetime]:
    log.info("Fetching Github access token for installation %d", installation_id)
    encoded_jwt = generate_jwt_token(app_id=app_id, pem=pem)
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {encoded_jwt}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def query() -> requests.Response:
        return requests.post(url=url, headers=headers, timeout=20)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    if response.status_code != 201:
        raise GithubException(
            f"Github returned status code {response.status_code} "
            "when creating new access token"
        )
    resp_body = response.json()
    access_token = resp_body["token"]
    assert isinstance(access_token, str)
    expires_iso8601 = resp_body["expires_at"]
    expires = datetime.fromisoformat(expires_iso8601)
    return access_token, expires


async def fetch_repository_list_for_github_app_installation(
    access_token: str,
) -> list[str]:
    log.info("Fetching repository list for Github installation")
    all_repos = []
    url = "https://api.github.com/installation/repositories?per_page=100"
    has_more = True
    while has_more:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        def query() -> requests.Response:
            return requests.get(url=url, headers=headers, timeout=20)

        response = await asyncio.get_event_loop().run_in_executor(None, query)
        if response.status_code != 200:
            raise GithubException(
                f"Github returned status code {response.status_code} "
                "when fetching repositories"
            )
        resp_body = response.json()
        all_repos += [repo["full_name"] for repo in resp_body["repositories"]]
        has_more = False
        link_header = response.headers.get("link")
        # <https://api.github.com/installation/repositories?page=2>; rel="next", ...
        if link_header is not None:
            for link in link_header.split(","):
                link_part, rel_part = link.split(";")
                if rel_part.strip() == 'rel="next"':
                    url = link_part.strip()[1:-1]
                    has_more = True
                    log.info(
                        "Fetching repository list for Github installation (next page)"
                    )
    return all_repos


def handle_app_created_on_github(pending_app_id: str, code: str) -> str:
    log.info("Handling returning user from Github app creation")
    url = f"https://api.github.com/app-manifests/{code}/conversions"
    response = requests.post(url, headers={"Accept": "application/json"}, timeout=120)
    resp_body = response.json()
    with Session.begin() as dbsession:
        pending_app = get_github_pending_app_by_id(dbsession, pending_app_id)
        assert pending_app is not None
        github_app = create_github_app(
            dbsession=dbsession,
            app_id=resp_body["id"],
            slug=resp_body["slug"],
            name=resp_body["name"],
            owner_id=resp_body["owner"]["id"],
            owner_login=resp_body["owner"]["login"],
            owner_type=resp_body["owner"]["type"],
            webhook_secret=resp_body["webhook_secret"],
            pem=resp_body["pem"],
            client_secret=resp_body["client_secret"],
            html_url=resp_body["html_url"],
            app_info=response.text,
        )
        delete_pending_github_app(dbsession, pending_app)
        owner_id = resp_body["owner"]["id"]
        install_url = (
            f"{github_app.html_url}/installations/new/permissions?target_id={owner_id}"
        )
        return install_url


async def repo_is_public(full_name: str) -> bool:
    log.info("Checking if Github repo is public %s", full_name)
    url = f"https://api.github.com/repos/{full_name}"

    def query() -> requests.Response:
        return requests.get(url, headers={"Accept": "application/json"}, timeout=120)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    return response.status_code == 200
