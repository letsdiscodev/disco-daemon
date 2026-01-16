import asyncio
import logging
import time

log = logging.getLogger(__name__)


async def clean_up_orphan_commands(remove_all: bool = False) -> None:
    """Clean up orphaned command containers.

    A command container is considered orphaned if:
    1. It has the disco.run=true label
    2. Either remove_all=True OR its expires timestamp has passed

    When remove_all=True (used on startup), ALL command containers are removed
    since any container existing before disco started is by definition orphaned.

    The expires label is set when the container is created, providing
    a simple TTL-based cleanup that works even without DB state.

    Returns the number of containers cleaned up.

    """
    log.info("Checking for orphaned command containers (remove_all=%s)", remove_all)

    # List all command containers
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "ps",
        "-a",
        "--filter",
        "label=disco.run=true",
        "--format",
        '{{.ID}}\t{{.Names}}\t{{.Label "disco.run.expires"}}\t{{.State}}',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        log.error("Failed to list command containers: %s", stderr.decode())
        return

    now = int(time.time())
    to_remove = []

    for line in stdout.decode().strip().split("\n"):
        if not line:
            continue

        parts = line.split("\t")
        if len(parts) < 4:
            continue

        _, name, expires_str, _ = parts

        # On startup, remove ALL command containers (they're orphans)
        if remove_all:
            log.info("Orphaned command container %s (startup cleanup)", name)
            to_remove.append(name)
            continue

        try:
            expires = int(expires_str) if expires_str else 0
        except ValueError:
            expires = 0

        # If expires is 0/missing, clean it up
        if expires == 0:
            log.warning(
                "Command container %s missing expires label, will be cleaned up", name
            )
            to_remove.append(name)
            continue

        if now > expires:
            log.info(
                "Command container %s expired (%d seconds ago)",
                name,
                now - expires,
            )
            to_remove.append(name)

    for name in to_remove:
        try:
            log.info("Removing command container: %s", name)
            await _remove_command_container(name)
        except Exception as e:
            log.warning("Failed to remove command container %s: %s", name, e)

    if to_remove:
        log.info("Cleaned up %d command containers", len(to_remove))
    else:
        log.debug("No command containers to clean up")

    return


async def _remove_command_container(name: str) -> None:
    """Stop and remove a command container."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "stop",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "rm",
        "-f",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
