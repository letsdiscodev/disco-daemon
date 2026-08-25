from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import NamedTuple

from disco.utils.backup import BACKUP_DIR, LATEST_FILENAME

log = logging.getLogger(__name__)

# Next to the backups so it is host-mounted and survives container restarts.
EMERGENCY_AUTH_LOG = BACKUP_DIR / "emergency-auth.log"


class CachedApiKey(NamedTuple):
    id: str
    name: str
    public_key: str


def append_emergency_auth_audit(
    api_key_id: str, public_key: str, reason: str
) -> None:
    try:
        EMERGENCY_AUTH_LOG.parent.mkdir(parents=True, exist_ok=True)
        # Never write the full key to disk.
        prefix = (api_key_id or "")[:8] + "..."
        ts = datetime.now(timezone.utc).isoformat()
        with open(EMERGENCY_AUTH_LOG, "a") as f:
            f.write(
                f'{{"ts": "{ts}", "api_key_prefix": "{prefix}", '
                f'"public_key": "{public_key}", "reason": "{reason}"}}\n'
            )
    except Exception:
        log.exception("Failed to write emergency-auth audit entry")


def find_api_key_by_id_in_backup(api_key_id: str) -> CachedApiKey | None:
    # TODO: once API keys are hashed at rest, compare against secret_hash
    # instead of looking up by plaintext id.
    path = BACKUP_DIR / LATEST_FILENAME
    if not path.exists():
        log.warning("Emergency auth: no local backup file at %s", path)
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT id, name, public_key, deleted FROM api_keys WHERE id=?",
            (api_key_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if row[3] is not None:
        return None
    return CachedApiKey(id=row[0], name=row[1], public_key=row[2])
