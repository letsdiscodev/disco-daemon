import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import Literal, Sequence

import requests
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import (
    ApiKey,
    GithubApp,
    GithubAppInstallation,
    GithubAppRepo,
    PendingGithubApp,
)
from disco.models.db import Session

log = logging.getLogger(__name__)


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
    pending_app.state = token_hex(16)


def get_github_pending_app_by_id(
    dbsession: DBSession, pending_app_id: str
) -> PendingGithubApp | None:
    return dbsession.get(PendingGithubApp, pending_app_id)


def delete_pending_github_app(dbsession, pending_app: PendingGithubApp) -> None:
    dbsession.delete(pending_app)


def handle_app_created_on_github(pending_app_id: str, code: str) -> str:
    url = f"https://api.github.com/app-manifests/{code}/conversions"
    response = requests.post(url, headers={"Accept": "application/json"}, timeout=120)
    resp_body = response.json()
    with Session.begin() as dbsession:
        pending_app = get_github_pending_app_by_id(dbsession, pending_app_id)
        assert pending_app is not None
        github_app = create_github_app(
            dbsession=dbsession,
            id=resp_body["id"],
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


def create_github_app(
    dbsession: DBSession,
    id: int,
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
    github_app = GithubApp(
        id=id,
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


def get_github_app_by_id(dbsession: DBSession, app_id: int) -> GithubApp | None:
    return dbsession.query(GithubApp).get(app_id)


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
        github_app = get_github_app_by_id(
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
    if x_github_event == "push":
        from disco.utils.deployments import create_deployment_sync
        from disco.utils.github import get_commit_info_from_webhook_push
        from disco.utils.mq.tasks import enqueue_task_deprecated
        from disco.utils.projects import get_project_by_github_app_repo

        try:
            full_name = body["repository"]["full_name"]
            branch, commit_hash = get_commit_info_from_webhook_push(body_text)
            if branch not in ["master", "main"]:
                log.info("Branch was not master or main, skipping")
                return
            with Session.begin() as dbsession:
                # TODO support multiple projects using same repo
                project = get_project_by_github_app_repo(dbsession, full_name)
                if project is None:
                    log.info("REPO NOT FOUND TODO")
                    return
                deployment = create_deployment_sync(
                    dbsession=dbsession,
                    project=project,
                    commit_hash=commit_hash,
                    disco_file=None,
                    by_api_key=None,
                )
                deployment_id = deployment.id
            enqueue_task_deprecated(
                task_name="PROCESS_DEPLOYMENT",
                body=dict(
                    deployment_id=deployment_id,
                ),
            )

        except KeyError:
            log.info("Not able to extract key info from Github webook, skipping")
            return
    else:
        log.info(
            "THIS IS THE EVENT: %s. We should be specific about it", x_github_event
        )
        try:
            action = body["action"]
            app_id = body["installation"]["app_id"]
            installation_id = body["installation"]["id"]
        except KeyError:
            log.info("Not able to extract key info from Github webook, skipping")
            return
        with Session.begin() as dbsession:
            installation: GithubAppInstallation | None
            github_app = get_github_app_by_id(dbsession, app_id)
            if github_app is None:
                log.warning("TODO LOGGING")
                return
            if action == "created":
                # app installed
                installation = create_github_app_installation(
                    dbsession, github_app, installation_id
                )
                for repo in body["repositories"]:
                    add_repository_to_installation(
                        dbsession, installation, repo["full_name"]
                    )
            elif action == "deleted":
                # app uninstalled
                installation = get_github_app_installation_by_id(
                    dbsession, installation_id
                )
                if installation is None:
                    log.warning("TODO LOGGING")
                    return
                delete_github_app_installation(dbsession, installation)
            elif action in ["added", "removed"]:
                # repos added/removed to/from app
                installation = get_github_app_installation_by_id(
                    dbsession, installation_id
                )
                if installation is None:
                    log.warning("TODO LOGGING")
                    return
                for repo in body["repositories_added"]:
                    add_repository_to_installation(
                        dbsession, installation, repo["full_name"]
                    )
                for repo in body["repositories_removed"]:
                    remove_repository_from_installation(
                        dbsession, installation, repo["full_name"]
                    )
            else:
                log.warning(
                    "Github App webhook action not handled %s, skipping", action
                )


def create_github_app_installation(
    dbsession: DBSession, github_app: GithubApp, installation_id: int
) -> GithubAppInstallation:
    # TODO do not create installation if installation with ID already exists
    installation = GithubAppInstallation(
        id=installation_id,
        github_app=github_app,
    )
    dbsession.add(installation)
    return installation


def get_github_app_installation_by_id(
    dbsession: DBSession, installation_id: int
) -> GithubAppInstallation | None:
    return dbsession.query(GithubAppInstallation).get(installation_id)


def add_repository_to_installation(
    dbsession: DBSession, installation: GithubAppInstallation, repo_full_name: str
) -> GithubAppRepo:
    # TODO get repo first and only add it if it's not there yet
    repo = GithubAppRepo(
        id=uuid.uuid4().hex,
        full_name=repo_full_name,
        installation=installation,
    )
    dbsession.add(repo)
    return repo


def remove_repository_from_installation(
    dbsession: DBSession, installation: GithubAppInstallation, repo_full_name: str
) -> None:
    dbsession.query(GithubAppRepo).filter(
        GithubAppRepo.installation == installation
    ).filter(GithubAppRepo.full_name == repo_full_name).delete()


def delete_github_app_installation(
    dbsession: DBSession, installation: GithubAppInstallation
) -> None:
    dbsession.query(GithubAppRepo).filter(
        GithubAppRepo.installation == installation
    ).delete()
    dbsession.delete(installation)


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
