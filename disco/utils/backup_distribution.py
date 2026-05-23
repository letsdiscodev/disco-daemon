"""Fan a backup file out to every manager's local disk via a Swarm global-job.

The daemon already has the backup on its own host (it just wrote it).
We dispatch a one-shot service (--mode global-job, constrained to
managers) where each task curl-pulls the file from the daemon's
internal HTTP endpoint and writes it to that manager's host.

Why HTTP pull rather than env-var push: SQLite backup files routinely
exceed safe env-var sizes (and Docker config / secret limits). HTTP
pull on the disco-main overlay scales to any reasonable backup size.

Token machinery: a fresh short-lived bearer is minted per push, passed
to the job via env var, and revoked when the job is removed.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import disco
from disco.models.db import AsyncSession
from disco.utils import keyvalues
from disco.utils.backup_tokens import backup_tokens
from disco.utils.subprocess import call, check_call

log = logging.getLogger(__name__)


async def push_backup_to_all_managers(
    backup_filename: str,
    *,
    is_periodic: bool = True,
    timeout_seconds: int = 300,
) -> None:
    """Distribute the named backup file to every manager.

    Assumes the file already exists at /disco/backups/<backup_filename>
    on the daemon's host (the daemon just wrote it). After this call
    returns, the same file exists on every manager's host with the
    matching name, latest.db is refreshed everywhere, and (for periodic
    backups) thinning has run on each receiver.
    """
    job_name = f"disco-backup-push-{uuid.uuid4().hex[:12]}"
    token = await backup_tokens.issue(ttl_seconds=timeout_seconds + 60)

    async with AsyncSession.begin() as dbsession:
        host_home = await keyvalues.get_value_str(dbsession, "HOST_HOME")

    image = f"letsdiscodev/daemon:{disco.__version__}"
    args = [
        "docker", "service", "create",
        "--name", job_name,
        "--mode", "global-job",
        "--constraint", "node.role==manager",
        "--network", "disco-main",
        "--mount", f"type=bind,source={host_home}/disco/backups,target=/cache",
        "--env", "BACKUP_URL=http://disco/api/disco/internal/backups",
        "--env", f"BACKUP_TOKEN={token}",
        "--env", f"BACKUP_FILENAME={backup_filename}",
        "--env", f"BACKUP_KIND={'periodic' if is_periodic else 'special'}",
        "--restart-condition", "none",
        "--log-driver", "json-file",
        "--log-opt", "max-size=20m",
        image,
        "python", "-m", "disco.scripts.pull_backup",
    ]

    log.info("Dispatching backup-push job %s for %s", job_name, backup_filename)
    try:
        await check_call(args)
        await _wait_for_global_job(job_name, timeout_seconds=timeout_seconds)
        log.info("Backup-push %s complete", job_name)
    finally:
        await backup_tokens.revoke(token)
        await call(["docker", "service", "rm", job_name])


async def cleanup_orphaned_push_jobs() -> None:
    """Remove backup-push global-job services left over from a previous
    daemon process. A daemon crash mid-push leaves the in-memory bearer
    token gone and the `finally`-block `service rm` un-run, so the job
    service lingers. Run this on daemon startup before issuing new
    pushes, otherwise these dead services pile up.
    """
    try:
        stdout, _, _ = await check_call([
            "docker", "service", "ls",
            "--filter", "name=disco-backup-push-",
            "--format", "{{.Name}}",
        ])
    except Exception:
        log.exception("Failed to list backup-push services for cleanup")
        return
    for svc in stdout:
        if svc.startswith("disco-backup-push-"):
            log.info("Removing orphan backup-push service %s", svc)
            await call(["docker", "service", "rm", svc])


async def _wait_for_global_job(job_name: str, timeout_seconds: int) -> None:
    """Block until every task of a global-job service has finished.

    `docker service ps` on a global-job shows tasks in "Complete" state
    once they exit successfully. We declare success when no task is
    still Running and no task is Failed/Rejected.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        stdout, _, _ = await check_call([
            "docker", "service", "ps", job_name,
            "--format", "{{.DesiredState}}|{{.CurrentState}}",
        ])
        if not stdout:
            await asyncio.sleep(1)
            continue

        running = 0
        failed = 0
        complete = 0
        for line in stdout:
            try:
                desired, current = line.split("|", 1)
            except ValueError:
                continue
            if current.startswith("Failed") or current.startswith("Rejected"):
                failed += 1
            elif current.startswith("Complete"):
                complete += 1
            elif desired == "Shutdown" and current.startswith("Shutdown"):
                complete += 1
            else:
                running += 1

        if failed:
            raise Exception(
                f"backup-push job {job_name} had {failed} failed tasks: {stdout}"
            )
        if running == 0 and complete > 0:
            return
        await asyncio.sleep(2)
    raise Exception(
        f"backup-push job {job_name} did not complete within {timeout_seconds}s"
    )
