"""Authenticate API keys against the local backup file when dqlite is locked.

This is the fallback path for endpoints that must remain reachable
during a cluster outage (status, recover-quorum). Reads the
read-only backup at /disco/backups/latest.db and looks up the API
key by id.

TODO(security-branch): once API keys are hashed at rest, this needs to
hash the presented key with the cluster's HMAC secret and compare
against `secret_hash` instead of looking up by plaintext id.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import NamedTuple

from disco.utils.backup import BACKUP_DIR, LATEST_FILENAME

log = logging.getLogger(__name__)

# Local audit trail of emergency-auth grants. Lives next to the backup
# file (already host-mounted) so it survives daemon container restarts.
# Each line is a JSON-ish single-record entry; format is intentionally
# simple so it can be tailed during incident response.
EMERGENCY_AUTH_LOG = BACKUP_DIR / "emergency-auth.log"


class CachedApiKey(NamedTuple):
    id: str
    name: str
    public_key: str


def append_emergency_auth_audit(
    api_key_id: str, public_key: str, reason: str
) -> None:
    """Append a line to the emergency-auth audit log. Best-effort.

    Used as a forensic trail since the DB path's record_api_key_usage
    isn't reachable during cluster lockup. Operators can inspect after
    recovery to see which keys were exercised during the outage.
    """
    try:
        EMERGENCY_AUTH_LOG.parent.mkdir(parents=True, exist_ok=True)
        # Truncate the id to its first 8 chars for readability — never
        # log the full secret to disk.
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
    """Look up an API key in the local backup file. Returns None if
    the file doesn't exist, the key isn't there, or the key has been
    soft-deleted.
    """
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
    # Soft-deleted keys must not authenticate.
    if row[3] is not None:
        return None
    return CachedApiKey(id=row[0], name=row[1], public_key=row[2])
