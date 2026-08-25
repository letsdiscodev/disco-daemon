from __future__ import annotations

import asyncio
import logging
import time
import uuid

import disco
from disco.models.db import AsyncReadSession
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
    job_name = f"disco-backup-push-{uuid.uuid4().hex[:12]}"

    # One use per manager, plus slack so a Swarm task retry doesn't 401.
    manager_count = await _count_manager_nodes()
    token = await backup_tokens.issue(
        uses=max(manager_count + 2, 2),
        ttl_seconds=timeout_seconds + 60,
    )

    async with AsyncReadSession.begin() as dbsession:
        host_home = await keyvalues.get_value_str(dbsession, "HOST_HOME")

    # Tasks pull over HTTP; backup files exceed env-var and Docker config limits.
    image = disco.daemon_image()
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


async def _count_manager_nodes() -> int:
    stdout, _, _ = await check_call([
        "docker", "node", "ls",
        "--filter", "role=manager",
        "--format", "{{.ID}}",
    ])
    return len(stdout) or 1


async def cleanup_orphaned_push_jobs() -> None:
    # A daemon crash mid-push skips the service rm in push_backup_to_all_managers.
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
