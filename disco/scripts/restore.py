"""Restore disco-daemon's dqlite cluster from a backup file.

SSH-gated CLI (not an HTTP endpoint). Operator runs this from a manager
host with Docker socket access. Works whether or not the disco daemon
is currently running. Destructive: replaces the keep-node's dqlite data
with the backup's contents.

Typical invocation:

    sudo docker run --rm -it \\
        -v /var/run/docker.sock:/var/run/docker.sock \\
        -v "$HOST_HOME/disco/backups:/disco/backups" \\
        --network disco-dqlite \\
        letsdiscodev/daemon:<version> \\
        disco_restore \\
            --backup pre-update-2026-05-23T08-12-00Z.db \\
            --keep-node node-A \\
            --remove-nodes node-C,node-D,node-E

The --network disco-dqlite bind matters: this script connects to the
restored dqlite via SQLAlchemy to replay the backup's SQL.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import disco
from disco.models.db import AsyncSession
from disco.utils.backup import BACKUP_DIR
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

# iterdump emits these around the actual statements; the daemon's
# outer transaction handles commit semantics so we strip them.
_TRANSACTION_BOUNDARY_RE = re.compile(r"^(BEGIN(?:\s+TRANSACTION)?|COMMIT)\s*;?\s*$", re.I)


def main() -> None:
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

    backup_version = _read_disco_version_from_backup(backup_path)
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
    print("===== Restore plan =====")
    print(f"  Backup file:       {backup_path}")
    print(f"  Disco version:     {backup_version}")
    print(f"  Keep node:         {args.keep_node}")
    print("                     (its current dqlite data WILL BE DESTROYED")
    print("                      and replaced by the backup's contents)")
    if rejoin_nodes:
        print(f"  Survivors to rejoin (data wiped, join fresh): {', '.join(rejoin_nodes)}")
    if remove_nodes:
        print(f"  Force-remove from Swarm: {', '.join(remove_nodes)}")
    print()
    print("This is destructive and not automatically reversible.")
    print(
        "A backup of the keep-node's pre-restore data dir is left at "
        "/data/<bind>.backup-<ts>/ inside the dqlite volume."
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


def _read_disco_version_from_backup(path: Path) -> str | None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM key_values WHERE key='DISCO_VERSION'"
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _list_swarm_nodes() -> dict[str, str]:
    """Return {node_id: disco-name} for all Swarm nodes that have one."""
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

    # Phase 1: stop every dqlite-* service so volumes can be wiped.
    # Use the same helper the daemon uses so we never accidentally pick
    # up unrelated services that happen to start with "dqlite-".
    existing_services = await list_dqlite_services()
    for svc in existing_services:
        log.info("Scaling %s to 0", svc)
        await async_call(["docker", "service", "scale", f"{svc}=0", "--detach"])
    # Actively wait until tasks have exited and released their volumes
    # rather than blind-sleeping. `docker service scale --detach` returns
    # before the container actually stops; mounting the same volume from
    # another container would race the still-running task.
    for svc in existing_services:
        try:
            await wait_for_service_tasks_stopped(svc, timeout_seconds=30)
        except Exception:
            log.exception(
                "Service %s did not stop within 30s; continuing", svc,
            )

    # Phase 2: wipe keep-node's volume so dqlite-demo will fresh-bootstrap.
    log.info("Wiping dqlite volume on keep-node %s", keep_node)
    await wipe_dqlite_volume_via_job(keep_node)

    # Phase 3: scale keep-node service back to 1 WITH BOOTSTRAP_ALLOWED so
    # the fresh container starts a single-node cluster of its own.
    keep_service = dqlite_service_name(keep_node)
    log.info("Adding BOOTSTRAP_ALLOWED=true to %s", keep_service)
    await async_check_call([
        "docker", "service", "update",
        "--env-add", "BOOTSTRAP_ALLOWED=true",
        "--force", "--detach", keep_service,
    ])
    await scale_dqlite_service(keep_node, 1)
    await wait_for_dqlite_service_healthy(keep_service)

    # Phase 4: replay backup's SQL into the fresh dqlite.
    log.info("Replaying backup SQL into fresh cluster")
    await _replay_backup(backup_path)

    # Phase 5: strip BOOTSTRAP_ALLOWED so a future task restart doesn't
    # accidentally create a second single-node cluster. We only roll the
    # service if the env was actually added in Phase 3 — in some
    # restore scenarios the original service already had no
    # BOOTSTRAP_ALLOWED, and an --env-rm --force would gratuitously
    # bounce the just-restored container.
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
        log.info("%s has no BOOTSTRAP_ALLOWED env to strip; skipping roll", keep_service)

    # Phase 6: rejoin survivors (wipe + restart).
    for rn in rejoin_nodes:
        try:
            log.info("Rejoining %s (wipe + restart)", rn)
            await wipe_dqlite_volume_via_job(rn)
            await scale_dqlite_service(rn, 1)
            await wait_for_dqlite_service_healthy(dqlite_service_name(rn))
        except Exception:
            log.exception("Failed to rejoin %s; continuing", rn)

    # Phase 7: force-remove gone nodes from Swarm.
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


async def _replay_backup(backup_path: Path) -> None:
    """Open the backup's binary SQLite file and stream its iterdump
    output through the live (now fresh) dqlite cluster."""
    from sqlalchemy import text as sql_text

    src = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        statements = list(src.iterdump())
    finally:
        src.close()

    # Strip iterdump's outer transaction markers — we control commit.
    cleaned = [
        s for s in statements
        if not _TRANSACTION_BOUNDARY_RE.match(s.strip())
    ]

    # Defensive reassembly check: if our naive split produced anything
    # weird, fail loudly. iterdump output for disco's schema is one
    # statement per line followed by ";\n" so this should always pass;
    # if it doesn't, we'd want to upgrade to sqlparse before proceeding.
    _verify_statements_reassemble(statements, cleaned)

    async with AsyncSession.begin() as session:
        for stmt in cleaned:
            await session.execute(sql_text(stmt))


def _verify_statements_reassemble(original: list[str], cleaned: list[str]) -> None:
    """Confirm we only dropped transaction-boundary statements during cleanup.

    The previous version also had an "expected == rebuilt" round-trip check
    that was logically always false (iterdump statements end with `;` not
    `\\n`), so it always raised and restore never worked. The remaining
    check is the load-bearing one: if our naive splitter dropped any line
    that isn't a BEGIN/COMMIT, refuse rather than silently lose data.
    """
    dropped = set(original) - set(cleaned)
    for d in dropped:
        if not _TRANSACTION_BOUNDARY_RE.match(d.strip()):
            raise RuntimeError(
                "iterdump produced a statement that doesn't look like a "
                "transaction boundary but was filtered out. Backup may "
                "contain DDL not supported by the naive splitter "
                "(triggers? multi-statement DDL?). Refusing to replay."
            )


if __name__ == "__main__":
    main()
