import asyncio
import logging
import time

log = logging.getLogger(__name__)


async def clean_up_orphan_shells(remove_all: bool = False) -> int:
    """
    Clean up orphaned shell containers.

    A shell container is considered orphaned if:
    1. It has the disco.shell=true label
    2. Either remove_all=True OR its expires_at timestamp has passed

    When remove_all=True (used on startup), ALL shell containers are removed
    since any container existing before disco started is by definition orphaned.

    The expires_at label is set when the container is created, providing
    a simple TTL-based cleanup that works even without DB state.

    Returns the number of containers cleaned up.
    """
    log.info("Checking for orphaned shell containers (remove_all=%s)", remove_all)

    # List all shell containers
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "ps",
        "-a",
        "--filter",
        "label=disco.shell=true",
        "--format",
        '{{.ID}}\t{{.Names}}\t{{.Label "disco.shell.expires_at"}}\t{{.State}}',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        log.error("Failed to list shell containers: %s", stderr.decode())
        return 0

    now = int(time.time())
    to_remove = []

    for line in stdout.decode().strip().split("\n"):
        if not line:
            continue

        parts = line.split("\t")
        if len(parts) < 4:
            continue

        container_id, name, expires_at_str, state = parts

        # On startup, remove ALL shell containers (they're orphans)
        if remove_all:
            log.info("Orphaned shell container %s (startup cleanup)", name)
            to_remove.append(name)
            continue

        try:
            expires_at = int(expires_at_str) if expires_at_str else 0
        except ValueError:
            expires_at = 0

        # If expires_at is 0/missing, clean it up
        if expires_at == 0:
            log.warning(
                "Shell container %s missing expires_at label, will be cleaned up", name
            )
            to_remove.append(name)
            continue

        if now > expires_at:
            log.info(
                "Shell container %s expired (%d seconds ago)",
                name,
                now - expires_at,
            )
            to_remove.append(name)

    # Remove containers
    for name in to_remove:
        try:
            log.info("Removing shell container: %s", name)
            await _remove_shell_container(name)
        except Exception as e:
            log.warning("Failed to remove shell container %s: %s", name, e)

    if to_remove:
        log.info("Cleaned up %d shell containers", len(to_remove))
    else:
        log.debug("No shell containers to clean up")

    return len(to_remove)


async def _remove_shell_container(name: str) -> None:
    """Stop and remove a shell container."""
    # First try graceful stop (will use the --stop-timeout from container config)
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "stop",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Then remove (may already be gone due to --rm)
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "rm",
        "-f",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
