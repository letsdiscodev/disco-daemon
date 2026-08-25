from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from disco.utils.backup import BACKUP_DIR
from disco.utils.backup_distribution import backup_tokens

log = logging.getLogger(__name__)

router = APIRouter()

_bearer = HTTPBearer(auto_error=False)


@router.get("/api/disco/internal/backups/{filename}")
async def serve_backup(
    filename: str,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
):
    if credentials is None:
        raise HTTPException(401, "missing bearer token")
    if not await backup_tokens.validate(credentials.credentials):
        raise HTTPException(401, "invalid or expired token")

    if (
        "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or filename.startswith(".")
        or filename in ("", ".", "..")
    ):
        raise HTTPException(400, "invalid filename")
    if not filename.endswith(".db"):
        raise HTTPException(400, "invalid filename")

    # Resolve so a symlink planted in the backup dir can't escape it.
    target = (BACKUP_DIR / filename).resolve()
    backup_root = BACKUP_DIR.resolve()
    try:
        target.relative_to(backup_root)
    except ValueError:
        raise HTTPException(400, "invalid filename")

    if not target.is_file():
        raise HTTPException(404, "backup not found")

    return FileResponse(
        target,
        media_type="application/octet-stream",
        filename=filename,
    )
