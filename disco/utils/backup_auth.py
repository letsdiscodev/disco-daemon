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
from typing import NamedTuple

from disco.utils.backup import BACKUP_DIR, LATEST_FILENAME

log = logging.getLogger(__name__)


class CachedApiKey(NamedTuple):
    id: str
    name: str
    public_key: str


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
