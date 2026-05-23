"""Receiver side of the backup distribution global-job.

Runs as a one-shot container on each manager (via --mode global-job).
Pulls the named backup file from the daemon's internal HTTP endpoint,
atomically lands it at /cache/<filename>, refreshes /cache/latest.db,
and (for periodic backups) thins old backups according to retention.

The bind mount maps the host's ${HOST_HOME}/disco/backups to /cache, so
writes here persist on the manager's host disk and become readable by
the daemon when it runs on that node.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    url = os.environ["BACKUP_URL"]
    token = os.environ["BACKUP_TOKEN"]
    filename = os.environ["BACKUP_FILENAME"]
    kind = os.environ.get("BACKUP_KIND", "periodic")

    cache = Path("/cache")
    cache.mkdir(parents=True, exist_ok=True)

    target = cache / filename
    log.info("Fetching %s/%s", url, filename)
    req = urllib.request.Request(
        f"{url}/{filename}",
        headers={"Authorization": f"Bearer {token}"},
    )
    tmp = target.with_suffix(target.suffix + ".tmp")
    # Bounded timeout so a hung HTTP server doesn't deadlock the Swarm
    # task indefinitely (which would in turn make the parent push job's
    # _wait_for_global_job hit its own timeout much later).
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
    tmp.replace(target)
    log.info("Wrote %s (%d bytes)", target, target.stat().st_size)

    latest = cache / "latest.db"
    latest_tmp = latest.with_suffix(".db.tmp")
    shutil.copy2(target, latest_tmp)
    latest_tmp.replace(latest)
    log.info("Updated %s", latest)

    if kind == "periodic":
        from disco.utils.backup import thin

        deleted = thin(cache, datetime.now(timezone.utc))
        if deleted:
            log.info("Thinned %d old backups: %s", len(deleted), [f.name for f in deleted])
        else:
            log.info("Thinning: nothing to delete")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("pull_backup failed")
        sys.exit(1)
