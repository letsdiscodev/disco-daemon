"""Restore the dqlite cluster from a backup file.

Run from a manager host, with or without the daemon running. Destructive:
the keep node's dqlite data is wiped and replaced by the backup.

    sudo docker run --rm -it \\
        -v /var/run/docker.sock:/var/run/docker.sock \\
        -v "$HOST_HOME/disco/backups:/disco/backups" \\
        --network disco-dqlite \\
        letsdiscodev/daemon:<version> \\
        disco_restore \\
            --backup pre-update-2026-05-23T08-12-00Z.db \\
            --keep-node node-A \\
            --remove-nodes node-C,node-D,node-E

--network disco-dqlite is required: the backup is replayed over SQLAlchemy.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path

import disco
from disco import config
from disco.utils.backup import (
    BACKUP_DIR,
    read_keyvalue_from_sqlite_file,
    replay_sqlite_file,
)
from disco.utils.dqlite import (
    dqlite_service_name,
    list_dqlite_services,
    scale_dqlite_service,
    wait_for_dqlite_service_healthy,
    wait_for_service_tasks_stopped,
    wipe_dqlite_volume_via_job,
)
from disco.utils.subprocess import call as async_call
from disco.utils.subprocess import check_call as async_check_call

log = logging.getLogger(__name__)

CONFIRM_TOKEN = "RESTORE"


def main() -> None:
    # Runs in a one-shot container without disco-data, so pin the mode.
    config.pin_mode("dqlite")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Restore disco-daemon's dqlite cluster from a backup.",
    )
    parser.add_argument("--backup", required=True, help="Backup filename in /disco/backups/")
    parser.add_argument("--keep-node", required=True, help="disco-name of the keepNode")
    parser.add_argument(
        "--remove-nodes", default="",
        help="Comma-separated disco-names to force-remove from Swarm (optional)",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the interactive confirmation prompt (use with care)",
    )
    args = parser.parse_args()

    backup_path = BACKUP_DIR / args.backup
    if not backup_path.is_file():
        print(f"ERROR: backup file not found: {backup_path}", file=sys.stderr)
        sys.exit(2)

    backup_version = read_keyvalue_from_sqlite_file(backup_path, "DISCO_VERSION")
    if backup_version != disco.__version__:
        print(
            f"ERROR: backup was created with disco {backup_version!r}; this "
            f"daemon image is {disco.__version__!r}. Restore on a matching version.",
            file=sys.stderr,
        )
        sys.exit(3)

    remove_nodes = [n.strip() for n in args.remove_nodes.split(",") if n.strip()]
    swarm_names_by_id = _list_swarm_nodes()
    swarm_names = set(swarm_names_by_id.values())

    if args.keep_node not in swarm_names:
        print(
            f"ERROR: --keep-node {args.keep_node!r} is not in the Swarm.",
            file=sys.stderr,
        )
        sys.exit(4)
    if args.keep_node in remove_nodes:
        print("ERROR: --keep-node cannot also be in --remove-nodes", file=sys.stderr)
        sys.exit(4)

    bad_removes = [rn for rn in remove_nodes if rn not in swarm_names]
    if bad_removes:
        print(
            f"ERROR: --remove-nodes contains unknown nodes: {bad_removes}",
            file=sys.stderr,
        )
        sys.exit(4)

    rejoin_nodes = sorted(swarm_names - {args.keep_node} - set(remove_nodes))

    print()
    print("Restore plan")
    print(f"  Backup file:       {backup_path}")
    print(f"  Disco version:     {backup_version}")
    print(f"  Keep node:         {args.keep_node}")
    print("                     (current dqlite data is wiped and")
    print("                      replaced by the backup)")
    if rejoin_nodes:
        print(f"  Survivors to rejoin (data wiped, join fresh): {', '.join(rejoin_nodes)}")
    if remove_nodes:
        print(f"  Force-remove from Swarm: {', '.join(remove_nodes)}")
    print()
    print("This is destructive and not reversible.")
    print(
        "The keep node's current dqlite data is not backed up; take a backup "
        "first if you need it."
    )
    print()

    if not args.yes:
        try:
            confirm = input(f"Type {CONFIRM_TOKEN} to proceed: ").strip()
        except EOFError:
            print("Aborted (no input).", file=sys.stderr)
            sys.exit(1)
        if confirm != CONFIRM_TOKEN:
            print("Aborted.", file=sys.stderr)
            sys.exit(1)

    asyncio.run(_do_restore(
        backup_path=backup_path,
        keep_node=args.keep_node,
        rejoin_nodes=rejoin_nodes,
        remove_nodes=remove_nodes,
        swarm_names_by_id=swarm_names_by_id,
    ))
    print("Restore complete.")


def _list_swarm_nodes() -> dict[str, str]:
    """{node_id: disco-name} for Swarm nodes that have one."""
    out = subprocess.check_output(
        ["docker", "node", "ls", "--format", "{{.ID}}"],
        text=True,
    )
    node_ids = [line for line in out.splitlines() if line.strip()]
    result: dict[str, str] = {}
    for nid in node_ids:
        try:
            name = subprocess.check_output(
                ["docker", "node", "inspect", "--format",
                 '{{ index .Spec.Labels "disco-name" }}', nid],
                text=True,
            ).strip()
        except subprocess.CalledProcessError:
            continue
        if name:
            result[nid] = name
    return result


async def _do_restore(
    *,
    backup_path: Path,
    keep_node: str,
    rejoin_nodes: list[str],
    remove_nodes: list[str],
    swarm_names_by_id: dict[str, str],
) -> None:
    started = time.monotonic()

    existing_services = await list_dqlite_services()
    for svc in existing_services:
        log.info("Scaling %s to 0", svc)
        await async_call(["docker", "service", "scale", f"{svc}=0", "--detach"])
    # scale --detach returns before tasks stop; the wipe job must not mount a
    # volume a task still holds.
    for svc in existing_services:
        try:
            await wait_for_service_tasks_stopped(svc, timeout_seconds=30)
        except Exception:
            log.exception(
                "Service %s did not stop within 30s; continuing", svc,
            )

    log.info("Wiping dqlite volume on keep-node %s", keep_node)
    await wipe_dqlite_volume_via_job(keep_node)

    # BOOTSTRAP_ALLOWED so the fresh container starts its own single-node cluster.
    keep_service = dqlite_service_name(keep_node)
    log.info("Adding BOOTSTRAP_ALLOWED=true to %s", keep_service)
    await async_check_call([
        "docker", "service", "update",
        "--env-add", "BOOTSTRAP_ALLOWED=true",
        "--force", "--detach", keep_service,
    ])
    await scale_dqlite_service(keep_node, 1)
    await wait_for_dqlite_service_healthy(keep_service)

    log.info("Replaying backup SQL into fresh cluster")
    await replay_sqlite_file(backup_path)

    # Only roll the service if the env is present; --env-rm --force would bounce
    # the restored container otherwise.
    inspect_out, _, _ = await async_check_call([
        "docker", "service", "inspect", "--format",
        "{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}",
        keep_service,
    ])
    has_bootstrap_env = any(
        line.startswith("BOOTSTRAP_ALLOWED=") for line in inspect_out
    )
    if has_bootstrap_env:
        log.info("Stripping BOOTSTRAP_ALLOWED from %s", keep_service)
        await async_check_call([
            "docker", "service", "update",
            "--env-rm", "BOOTSTRAP_ALLOWED",
            "--force", "--detach", keep_service,
        ])
        await wait_for_dqlite_service_healthy(keep_service)
    else:
        log.info("%s has no BOOTSTRAP_ALLOWED env; not rolling", keep_service)

    for rn in rejoin_nodes:
        try:
            log.info("Rejoining %s", rn)
            await wipe_dqlite_volume_via_job(rn)
            await scale_dqlite_service(rn, 1)
            await wait_for_dqlite_service_healthy(dqlite_service_name(rn))
        except Exception:
            log.exception("Failed to rejoin %s; continuing", rn)

    name_to_id = {n: i for i, n in swarm_names_by_id.items()}
    for rn in remove_nodes:
        node_id = name_to_id.get(rn)
        if node_id is None:
            continue
        try:
            await async_check_call(
                ["docker", "node", "rm", "--force", node_id]
            )
        except Exception:
            log.exception("Failed to force-remove %s", rn)

    elapsed = int((time.monotonic() - started) * 1000)
    log.info("Restore finished in %dms", elapsed)


if __name__ == "__main__":
    main()
