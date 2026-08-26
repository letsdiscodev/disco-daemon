"""Migrate a sqlite-mode install to dqlite mode.

Run on the main node. Moves the database and Caddy's certificates and config
into a single-node dqlite cluster, swaps Caddy for the dqlite-backed global
service and restarts the daemon in dqlite mode. HOST_HOME is usually /root.

    sudo docker run --rm -it \\
        -v /var/run/docker.sock:/var/run/docker.sock \\
        --mount source=disco-data,target=/disco/data \\
        --mount source=disco-caddy-data,target=/old-caddy-data,readonly \\
        --mount type=bind,source=$HOST_HOME/disco/backups,target=/disco/backups \\
        --mount type=bind,source=$HOST_HOME/disco/caddy-socket,target=/disco/caddy-socket \\
        letsdiscodev/daemon:<version> \\
        disco_migrate_to_dqlite

Do not pass --network or --hostname; the script attaches itself to disco-dqlite
using its container id as hostname. Optional: -e DISCO_IMAGE, -e CADDY_IMAGE.

The sqlite file and the disco-caddy-data / disco-caddy-config volumes are never
modified; on failure the script rolls back to sqlite mode using them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import disco
from disco import config
from disco.utils.backup import (
    BACKUP_DIR,
    read_keyvalue_from_sqlite_file,
    replay_sqlite_file,
)
from disco.utils.dqlite import list_dqlite_services

log = logging.getLogger(__name__)

CONFIRM_TOKEN = "MIGRATE"


class Preflight(NamedTuple):
    disco_version: str
    disco_host: str
    host_home: str
    tunnel_token: str | None
    node_id: str
    disco_name: str
    image: str
    caddy_config: dict[str, Any]

SQLITE_DB_PATH = Path("/disco/data/disco.sqlite3")
OLD_CADDY_DATA_MOUNT = Path("/old-caddy-data")


def main() -> None:
    # The sqlite side is only read through private sqlite3 connections.
    config.pin_mode("dqlite")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Migrate an existing sqlite-mode disco install to dqlite mode.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt (use with care)",
    )
    args = parser.parse_args()

    state = _preflight()

    print()
    print("Migration plan")
    print(f"  Disco version:  {state.disco_version}")
    print(f"  Disco host:     {state.disco_host}")
    print(f"  Node:           {state.disco_name or '(will be named)'}")
    print("  sqlite file, disco-caddy-data and disco-caddy-config volumes")
    print("  are left untouched.")
    print()
    print("The daemon restarts in dqlite mode; ingress is briefly")
    print("interrupted while Caddy is swapped.")
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

    _migrate(state)
    print("Migration complete; disco is running in dqlite mode.")


def _preflight() -> Preflight:
    from disco.utils import caddy

    # Read the marker file directly; pin_mode() hides the real mode.
    if config.DISCO_MODE_FILE and Path(config.DISCO_MODE_FILE).is_file():
        marker = Path(config.DISCO_MODE_FILE).read_text().strip()
        if marker == "dqlite":
            _fail("this install is already in dqlite mode")

    if not SQLITE_DB_PATH.is_file():
        _fail(
            f"sqlite database not found at {SQLITE_DB_PATH}; mount the "
            "disco-data volume"
        )
    if not OLD_CADDY_DATA_MOUNT.is_dir():
        _fail(
            f"{OLD_CADDY_DATA_MOUNT} not mounted; mount the disco-caddy-data "
            "volume there (readonly)"
        )
    if not BACKUP_DIR.is_dir():
        _fail(f"{BACKUP_DIR} not mounted; mount $HOST_HOME/disco/backups there")

    disco_version = _read_keyvalue("DISCO_VERSION")
    disco_host = _read_keyvalue("DISCO_HOST")
    host_home = _read_keyvalue("HOST_HOME")
    tunnel_token = _read_keyvalue("CLOUDFLARE_TUNNEL_TOKEN")
    if disco_version != disco.__version__:
        _fail(
            f"installed disco version is {disco_version!r} but this image is "
            f"{disco.__version__!r}; run 'disco update' first, then migrate "
            "with the matching image"
        )
    if disco_host is None or host_home is None:
        _fail("DISCO_HOST/HOST_HOME missing from the database")
    assert disco_version is not None
    assert disco_host is not None
    assert host_home is not None

    node_id, disco_name, is_main = _inspect_self_node()
    if not is_main:
        _fail("this node is not the disco main node (disco-role=main)")

    existing = asyncio.run(list_dqlite_services())
    if existing:
        _fail(
            f"dqlite services already exist ({', '.join(existing)}); remove "
            "them and their volumes, then re-run"
        )

    node_count = len(
        _run(["docker", "node", "ls", "--format", "{{.ID}}"]).splitlines()
    )
    if node_count > 1:
        print(
            "WARNING: multi-node Swarm. dqlite is bootstrapped on this node "
            "only; the reconciler adds the others once the daemon starts."
        )

    image = _daemon_service_image()

    # Capture the live Caddy config while the old Caddy is still running.
    try:
        caddy_config = caddy.get_config()
    except Exception as exc:
        _fail(f"could not read the live Caddy config over the admin socket: {exc}")
    assert caddy_config is not None

    return Preflight(
        disco_version=disco_version,
        disco_host=disco_host,
        host_home=host_home,
        tunnel_token=tunnel_token,
        node_id=node_id,
        disco_name=disco_name,
        image=image,
        caddy_config=caddy_config,
    )


def _migrate(state: Preflight) -> None:
    from disco.scripts.init import start_disco_daemon
    from disco.scripts.migrate_caddy_storage import migrate as migrate_caddy_data

    host_home = state.host_home
    tunnel = state.tunnel_token is not None
    caddy_swapped = False
    marker_flipped = False
    try:
        print("Taking a safety copy of the sqlite database")
        safety_copy = BACKUP_DIR / (
            "pre-dqlite-migration-"
            + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
            + ".db"
        )
        _copy_sqlite_file(SQLITE_DB_PATH, safety_copy)
        print(f"Safety copy at {safety_copy}")

        print("Stopping the disco daemon")
        _run(["docker", "service", "rm", "disco"], check=False)
        # Nothing may write to the sqlite file while it is copied.
        _wait_for_containers_stopped("disco.", "old disco daemon container")

        _bootstrap_dqlite(state)

        print("Copying data from sqlite into dqlite")
        asyncio.run(replay_sqlite_file(SQLITE_DB_PATH))

        print("Migrating TLS certificates into dqlite")
        migrated = migrate_caddy_data(str(OLD_CADDY_DATA_MOUNT / "caddy"))
        print(f"Migrated {migrated} certificate storage files")

        caddy_swapped = True
        _swap_caddy(state)

        print("Switching install to dqlite mode")
        config.write_disco_mode("dqlite")
        marker_flipped = True

        print("Starting the disco daemon in dqlite mode")
        start_disco_daemon(host_home, state.image)
        _wait_for_daemon_running()

        if tunnel:
            _reattach_cloudflare_tunnel()
    except BaseException:
        # BaseException so Ctrl+C mid-migration still rolls back.
        log.exception("Migration failed; rolling back to sqlite mode")
        _rollback(
            host_home=host_home,
            image=state.image,
            tunnel=tunnel,
            caddy_swapped=caddy_swapped,
            marker_flipped=marker_flipped,
        )
        sys.exit(1)


def _bootstrap_dqlite(state: Preflight) -> None:
    from disco.utils.dqlite import bootstrap_first_node_sync
    from disco.utils.randomname import generate_random_name_sync

    disco_name = state.disco_name
    if not disco_name:
        disco_name = generate_random_name_sync()
        print(f"Assigning disco-name to this node: {disco_name}")
        _run(
            [
                "docker",
                "node",
                "update",
                "--label-add",
                f"disco-name={disco_name}",
                state.node_id,
            ]
        )

    _run(
        [
            "docker",
            "network",
            "create",
            "--driver",
            "overlay",
            "--attachable",
            "--opt",
            "encrypted",
            "disco-dqlite",
        ],
        check=False,  # may exist from a previous attempt
    )
    # Attach this container so the SQL replay can reach dqlite; the container
    # id is the hostname.
    _run(["docker", "network", "connect", "disco-dqlite", socket.gethostname()])

    print("Starting dqlite")
    bootstrap_first_node_sync(disco_name)


def _swap_caddy(state: Preflight) -> None:
    from disco.scripts.init import start_caddy_service
    from disco.utils import caddy

    tunnel = state.tunnel_token is not None
    print("Swapping Caddy")
    _run(["docker", "container", "stop", "disco-caddy"], check=False)
    _run(["docker", "container", "rm", "disco-caddy"], check=False)
    start_caddy_service(
        state.host_home, tunnel=tunnel, caddy_image=config.get_caddy_image()
    )
    _wait_for_caddy_socket()
    print("Seeding the captured Caddy config")
    caddy.set_config(caddy.add_dqlite_config_fragments(state.caddy_config))


def _rollback(
    *,
    host_home: str,
    image: str,
    tunnel: bool,
    caddy_swapped: bool,
    marker_flipped: bool,
) -> None:
    from disco.scripts.init import start_caddy_container, start_disco_daemon
    from disco.utils import docker

    if marker_flipped:
        config.write_disco_mode("sqlite")
    config.pin_mode("sqlite")

    if caddy_swapped:
        print("Rollback: restoring the stock Caddy container")
        try:
            _run(["docker", "service", "rm", "disco-caddy"], check=False)
            # The container publishes the same ports as the global service's task.
            _wait_for_containers_stopped("disco-caddy.", "disco-caddy task")
            # --resume restores the pre-migration config from the untouched
            # disco-caddy-config volume.
            start_caddy_container(host_home, tunnel=tunnel)
            if tunnel:
                # With tunnel=True the container publishes no ports.
                docker.add_network_to_container_sync(
                    "disco-caddy", "disco-cloudflare-tunnel", alias="disco-server"
                )
        except Exception:
            log.exception(
                "Rollback: could not restore the Caddy container. Manually: "
                "docker service rm disco-caddy, re-run the old disco-caddy "
                "container, and with a Cloudflare tunnel: docker network "
                "connect --alias disco-server disco-cloudflare-tunnel disco-caddy"
            )
    print("Rollback: restarting the disco daemon in sqlite mode")
    try:
        _run(["docker", "service", "rm", "disco"], check=False)
        start_disco_daemon(host_home, image)
    except Exception:
        log.exception("Rollback: could not restart the daemon; restart it manually")
    print(
        "Rolled back to sqlite mode. Leftovers to clean up: the dqlite-* "
        "service and volume, and the disco-dqlite network."
    )


def _reattach_cloudflare_tunnel() -> None:
    # Does not survive the Caddy task being rescheduled.
    from disco.utils import docker

    print("Reattaching the Cloudflare tunnel to the new Caddy")
    try:
        docker.add_network_to_container_sync(
            docker.local_caddy_container_sync(),
            "disco-cloudflare-tunnel",
            alias="disco-server",
        )
    except Exception:
        log.exception(
            "Could not attach the Cloudflare tunnel network to the new Caddy. "
            "Manually: docker network connect --alias disco-server "
            "disco-cloudflare-tunnel <disco-caddy task container>"
        )


def _read_keyvalue(key: str) -> str | None:
    return read_keyvalue_from_sqlite_file(SQLITE_DB_PATH, key)


def _copy_sqlite_file(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _inspect_self_node() -> tuple[str, str, bool]:
    node_id = _run(
        ["docker", "node", "inspect", "--format", "{{ .ID }}", "self"]
    ).strip()
    disco_name = _run(
        [
            "docker",
            "node",
            "inspect",
            "--format",
            '{{ index .Spec.Labels "disco-name" }}',
            "self",
        ]
    ).strip()
    disco_role = _run(
        [
            "docker",
            "node",
            "inspect",
            "--format",
            '{{ index .Spec.Labels "disco-role" }}',
            "self",
        ]
    ).strip()
    return node_id, disco_name, disco_role == "main"



def _wait_for_caddy_socket() -> None:
    from disco.utils import caddy

    for _ in range(60):
        try:
            caddy.get_config()
            return
        except Exception:
            time.sleep(1)
    raise Exception("new Caddy did not become reachable over the admin socket")


def _daemon_service_image() -> str:
    # DISCO_IMAGE in this container is usually unset, and disco.daemon_image()
    # would then guess a Docker Hub tag; the running service knows the truth.
    override = os.environ.get("DISCO_IMAGE")
    if override:
        return override
    image = _run(
        [
            "docker",
            "service",
            "inspect",
            "disco",
            "--format",
            "{{.Spec.TaskTemplate.ContainerSpec.Image}}",
        ]
    ).strip()
    if not image:
        _fail("could not read the daemon image from the disco service")
    return image.split("@", 1)[0]


def _wait_for_containers_stopped(prefix: str, what: str) -> None:
    # Prefix-match names; a docker name filter substring-matches.
    for _ in range(120):
        names = _run(["docker", "ps", "--format", "{{.Names}}"]).splitlines()
        if not any(name.startswith(prefix) for name in names):
            return
        time.sleep(1)
    raise Exception(f"{what} did not stop")


def _wait_for_daemon_running() -> None:
    for _ in range(120):
        out = _run(
            [
                "docker",
                "service",
                "ps",
                "disco",
                "--filter",
                "desired-state=running",
                "--format",
                "{{.CurrentState}}",
            ],
            check=False,
        )
        if any(line.startswith("Running") for line in out.splitlines()):
            return
        time.sleep(2)
    raise Exception("disco service did not reach Running state")


def _run(args: list[str], check: bool = True) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise Exception(
            f"{' '.join(args)} returned {result.returncode}:\n"
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
