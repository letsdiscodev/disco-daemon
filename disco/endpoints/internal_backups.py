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

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse

from disco.utils.backup import BACKUP_DIR
from disco.utils.backup_tokens import backup_tokens

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/disco/internal/backups/{filename}")
async def serve_backup(
    filename: str,
    authorization: Annotated[str | None, Header()] = None,
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization[len("Bearer "):]
    if not await backup_tokens.validate(token):
        raise HTTPException(401, "invalid or expired token")

    # Filename sanitization: no path traversal, must look like a backup file.
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(400, "invalid filename")
    if not filename.endswith(".db"):
        raise HTTPException(400, "invalid filename")

    path = BACKUP_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "backup not found")

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
    )
