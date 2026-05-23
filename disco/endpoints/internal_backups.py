"""Internal endpoint that serves the daemon's local backup files.

Consumed only by the per-backup global-job that pushes backups out to
every manager. Auth is a short-lived bearer token issued by the daemon
just before dispatching the job. Not exposed to normal API consumers.

The endpoint lives under /api/disco/internal/ so it's clearly internal
machinery, not part of the operator-facing API.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from disco.utils.backup import BACKUP_DIR
from disco.utils.backup_tokens import backup_tokens

log = logging.getLogger(__name__)

router = APIRouter()

# auto_error=False so we raise our own 401 (so the body is consistent
# with the rest of disco's errors and not FastAPI's default WWW-Authenticate
# challenge).
_bearer = HTTPBearer(auto_error=False)


@router.get("/api/disco/internal/backups/{filename}")
async def serve_backup(
    filename: str,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
):
    # HTTPBearer handles case-insensitive scheme matching and rejects
    # multi-value Authorization headers automatically.
    if credentials is None:
        raise HTTPException(401, "missing bearer token")
    if not await backup_tokens.validate(credentials.credentials):
        raise HTTPException(401, "invalid or expired token")

    # Filename sanitization. We reject anything that could escape the
    # backup directory or that doesn't look like a backup file.
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

    # Resolve symlinks and verify the resolved path is actually inside
    # BACKUP_DIR. Defends against a symlink planted in the backup dir
    # by an attacker with host write access.
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
