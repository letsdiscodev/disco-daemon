import json
import logging
import time
from datetime import datetime, timezone
from html import escape
from typing import Annotated

import randomname
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Path,
    Request,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key, get_api_key_sync
from disco.endpoints.dependencies import get_db, get_sync_db
from disco.models import ApiKey, PendingGithubApp
from disco.models.db import Session
from disco.utils import keyvalues
from disco.utils.githubapps import (
    create_pending_github_app,
    generate_new_pending_app_state,
    get_all_github_apps,
    get_all_repos_sync,
    get_github_pending_app_by_id,
    handle_app_created_on_github,
    process_github_app_webhook,
)

log = logging.getLogger(__name__)

router = APIRouter()


def get_pending_app_from_url(
    pending_app_id: Annotated[str, Path()],
):
    with Session.begin() as dbsession:
        pending_app = get_github_pending_app_by_id(dbsession, pending_app_id)
        if pending_app is None:
            raise HTTPException(status_code=404)
        if pending_app.expires < datetime.now(timezone.utc):
            raise HTTPException(status_code=404)
        yield pending_app


def get_pending_app_id_from_url_with_state(
    pending_app_id: Annotated[str, Path()],
    state: str,
):
    with Session.begin() as dbsession:
        pending_app = get_github_pending_app_by_id(dbsession, pending_app_id)
        if pending_app is None:
            raise HTTPException(status_code=404)
        if pending_app.expires < datetime.now(timezone.utc):
            raise HTTPException(status_code=404)
        if pending_app.state != state:
            raise HTTPException(status_code=404)
        yield pending_app.id


class NewGithubAppRequestBody(BaseModel):
    organization: str | None = Field(None, pattern=r"^\S+$", max_length=255)


@router.post("/api/github-apps/create", status_code=201)
def github_app_create_post(
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key_sync)],
    req_body: NewGithubAppRequestBody,
):
    pending_app = create_pending_github_app(
        dbsession=dbsession, organization=req_body.organization, by_api_key=api_key
    )
    disco_host = keyvalues.get_value_sync(dbsession, "DISCO_HOST")
    assert disco_host is not None
    return {
        "pendingApp": {
            "id": pending_app.id,
            "expires": pending_app.expires.isoformat(),
            "url": f"https://{disco_host}/github-apps/{pending_app.id}/create",
        }
    }


CREATE_APP_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
</head>
<body>
    <form action="{github_url}" method="post" id="create-github-app-form">
        <input type="hidden" name="manifest" value="{manifest_data}"/>
        <input type="submit" value="Submit"/>
    </form>
    <script>
        document.getElementById('create-github-app-form').submit();
    </script>
</body>
</html>"""


@router.get(
    "/github-apps/{pending_app_id}/create",
    response_class=HTMLResponse,
)
def github_app_create_get(
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
    pending_app: Annotated[PendingGithubApp, Depends(get_pending_app_from_url)],
):
    disco_host = keyvalues.get_value_sync(dbsession, "DISCO_HOST")
    assert disco_host is not None
    generate_new_pending_app_state(pending_app)
    if pending_app.organization is not None:
        github_url = f"https://github.com/organizations/{pending_app.organization}/settings/apps/new?state={pending_app.state}"
    else:
        github_url = f"https://github.com/settings/apps/new?state={pending_app.state}"
    manifest = {
        "name": f"Disco {randomname.get_name()}",
        "url": f"https://{disco_host}/github-apps/home",
        "redirect_url": f"https://{disco_host}/github-apps/{pending_app.id}/created",
        "callback_urls": [],
        "hook_attributes": {
            "url": f"https://{disco_host}/.webhooks/github-apps",
        },
        "public": False,
        "default_permissions": {
            "contents": "read",
        },
        "default_events": ["push"],
    }
    return CREATE_APP_HTML.format(
        github_url=github_url, manifest_data=escape(json.dumps(manifest))
    )


@router.get(
    "/github-apps/{pending_app_id}/created",
    response_class=HTMLResponse,
)
def github_app_created_get(
    pending_app_id: Annotated[str, Depends(get_pending_app_id_from_url_with_state)],
    code: str,
):
    app_install_url = handle_app_created_on_github(
        pending_app_id=pending_app_id, code=code
    )
    # the app_install_url sometimes return 404 if we're too fast
    time.sleep(1)
    return RedirectResponse(url=app_install_url, status_code=302)


async def get_body(request: Request):
    return await request.body()


@router.get("/api/github-apps", dependencies=[Depends(get_api_key)])
async def list_github_apps(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
):
    github_apps = await get_all_github_apps(dbsession)
    return {
        "githubApps": [
            {
                "id": github_app.id,
                "owner": {
                    "id": github_app.owner_id,
                    "login": github_app.owner_login,
                    "type": github_app.owner_type,
                },
                "appUrl": github_app.html_url,
                "installUrl": f"{github_app.html_url}/installations"
                f"/new/permissions?target_id={github_app.owner_id}",
                "installation": {
                    "id": (await github_app.awaitable_attrs.installations)[0].id,
                    "manageUrl": "https://github.com/settings/installations"
                    f"/{(await github_app.awaitable_attrs.installations)[0].id}"
                    if github_app.owner_type == "User"
                    else f"https://github.com/organizations/{github_app.owner_login}"
                    f"/settings/installations/{(await github_app.awaitable_attrs.installations)[0].id}",
                }
                if len((await github_app.awaitable_attrs.installations)) > 0
                else None,
            }
            for github_app in github_apps
        ],
    }


@router.post("/.webhooks/github-apps", status_code=202)
def github_webhook_service_post(
    x_github_event: Annotated[str | None, Header()],
    x_hub_signature_256: Annotated[str | None, Header()],
    x_github_hook_installation_target_type: Annotated[str | None, Header()],
    x_github_hook_installation_target_id: Annotated[str | None, Header()],
    background_tasks: BackgroundTasks,
    body: Annotated[bytes, Depends(get_body)],
):
    log.info("Received Github webhook: %s", body.decode("utf-8"))

    def process_webhook() -> None:
        process_github_app_webhook(
            request_body_bytes=body,
            x_github_event=x_github_event,
            x_hub_signature_256=x_hub_signature_256,
            x_github_hook_installation_target_type=x_github_hook_installation_target_type,
            x_github_hook_installation_target_id=x_github_hook_installation_target_id,
        )

    background_tasks.add_task(process_webhook)
    return {}


@router.get("/api/github-app-repos", dependencies=[Depends(get_api_key_sync)])
def list_github_repos(
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
):
    repos = get_all_repos_sync(dbsession)
    return {
        "repos": [
            {
                "fullName": repo.full_name,
            }
            for repo in repos
        ],
    }
