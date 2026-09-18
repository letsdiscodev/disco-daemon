"""Script that runs when updating Disco to the latest version"""

import asyncio
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Awaitable, Callable

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import disco
from disco import config
from disco.models.db import ReadSession, Session, build_engines, get_engine
from disco.scripts.init import run_and_print, start_disco_daemon
from disco.utils import keyvalues
from disco.utils.meta import save_done_updating
from disco.utils.subprocess import call, check_call

log = logging.getLogger(__name__)


def main() -> None:
    asyncio.run(_main())


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    await build_engines()
    image = os.environ.get("DISCO_IMAGE")
    assert image is not None
    async with ReadSession.begin() as dbsession:
        installed_version = await keyvalues.get_value(
            dbsession=dbsession, key="DISCO_VERSION"
        )
        assert installed_version is not None
    if installed_version == disco.__version__:
        print(f"Current version is latest ({disco.__version__}), not updating.")
        await _start_daemon_if_missing(image)
        async with Session.begin() as dbsession:
            await save_done_updating(dbsession)
        return
    try:
        get_update_function_for_version(installed_version)
    except NotImplementedError:
        print(f"Updating from version {installed_version} is not supported.")
        async with Session.begin() as dbsession:
            await save_done_updating(dbsession)
        return
    print(f"Installed version: {installed_version}")
    print(f"New version: {disco.__version__}")
    print("Stopping existing Disco processes")
    try:
        await stop_disco_daemon()
    except Exception:
        log.info("Failed to stop Disco")
    print("Running upgrade tasks")
    ttl = 9999
    while installed_version != disco.__version__:
        assert installed_version is not None
        task = get_update_function_for_version(installed_version)
        try:
            await task(image)
        except Exception:
            # the daemon is stopped and the update is not done: the tasks
            # can be run again, they pick up where they left off
            log.exception("Update task failed")
            print(
                f"Updating from {installed_version} failed, Disco is not running. "
                "Run the update again from the server:\n"
                f"  {_rerun_command(image)}"
            )
            raise
        async with ReadSession.begin() as dbsession:
            installed_version = await keyvalues.get_value(
                dbsession=dbsession, key="DISCO_VERSION"
            )
        ttl -= 1
        if ttl < 0:
            print(
                f"Caught in an infinite loop while upgrading from {installed_version}"
            )
            break

    print("Starting new version of Disco")
    async with ReadSession.begin() as dbsession:
        host_home = await keyvalues.get_value(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    await start_disco_daemon(host_home, image)
    async with Session.begin() as dbsession:
        await save_done_updating(dbsession)


async def _start_daemon_if_missing(image: str) -> None:
    """an update that died after writing the version but before restarting the
    daemon leaves `disco` removed: running the update again must bring it back."""
    from disco.utils import docker

    if await docker.service_exists("disco"):
        return
    print("The Disco service is missing, starting it")
    async with ReadSession.begin() as dbsession:
        host_home = await keyvalues.get_value(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    await start_disco_daemon(host_home, image)


def _rerun_command(image: str) -> str:
    """The command the daemon ran (see disco.utils.meta.update_disco)."""
    from disco.utils.dqlite import DQLITE_OVERLAY_NETWORK

    network = f" --network {DQLITE_OVERLAY_NETWORK}" if config.is_ha() else ""
    return (
        f"docker run --rm{network} --env DISCO_IMAGE={image} "
        "--mount source=disco-data,target=/disco/data "
        "--mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock "
        f"{image} disco_update"
    )


async def stop_disco_daemon() -> None:
    await run_and_print(
        [
            "docker",
            "service",
            "rm",
            "disco",
        ]
    )


async def alembic_upgrade(version_hash: str) -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(_alembic_upgrade, version_hash)


def _alembic_upgrade(connection, version_hash: str) -> None:
    config = Config("/disco/app/alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, version_hash)


async def task_0_33_x(image: str) -> None:
    """logspout -> vector for every syslog destination (see docs/logging.md).

    per destination: create the vector collector, wait until it runs on every node,
    emit a marker line that must reach the destination through vector, THEN remove the
    logspout service. never remove-before-create. streaming collectors (`disco logs`)
    are removed, clients reconnect. idempotent and resumable: a run that dies at any
    point can be run again and finds the vector collectors it already created. the
    version is written last.
    """
    from disco.utils import docker
    from disco.utils.syslog import get_destination_buffer_bytes, get_syslog_urls

    print("Updating from 0.33.x to 0.34.0")
    async with ReadSession.begin() as dbsession:
        disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
        syslog_urls = await get_syslog_urls(dbsession)
        buffer_bytes = await get_destination_buffer_bytes(dbsession)
    print(f"Pulling {docker.vectorconfig.VECTOR_IMAGE}")
    await docker.pull(docker.vectorconfig.VECTOR_IMAGE)
    existing = await docker.list_syslog_services()
    skipped: set[tuple[str, str]] = set()
    for syslog_url in syslog_urls:
        url, type = syslog_url["url"], syslog_url["type"]
        try:
            config = docker.vectorconfig.render_syslog_config(
                url, type, buffer_bytes, disco_host
            )
        except docker.vectorconfig.InvalidSyslogUrl as e:
            print(
                f"SKIPPING {url} ({type}): {e}. Its logspout service is left running."
            )
            skipped.add((url, type))
            continue
        name = docker.syslog_service_name(url, type, config)
        if name not in {service.name for service in existing}:
            print(f"Starting the Vector collector for {url} ({type})")
            await docker.start_syslog_service(
                disco_host=disco_host, url=url, type=type, buffer_bytes=buffer_bytes
            )
        else:
            print(f"Vector collector for {url} ({type}) already exists")
        ready = await docker.wait_for_global_service(name, timeout=300)
        if not ready:
            raise Exception(
                f"The Vector collector {name} for {url} is not running on every node"
            )
        await _emit_logging_migration_marker(url, type)
        # the collector attaches to the containers a few seconds after its task runs
        await asyncio.sleep(docker.COLLECTOR_SETTLE_SECONDS)
        for service in existing:
            if service.url == url and service.type == type and service.impl is None:
                print(f"Removing the logspout service {service.name} for {url}")
                await docker.rm_syslog_service(service)
    # logspout services for destinations that are no longer configured
    for service in existing:
        if (service.url, service.type) in skipped:
            continue
        if service.impl is None and await docker.service_exists(service.name):
            print(f"Removing the logspout service {service.name} for {service.url}")
            await docker.rm_syslog_service(service)
    streaming = await docker.list_streaming_services()
    for name in streaming:
        print(f"Removing the streaming collector {name} (clients reconnect)")
        await docker.rm_service(name)
    await docker.wait_for_cleanups()
    await docker.prune_logging_configs()
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.34.0"
        )


async def _emit_logging_migration_marker(url: str, type: str) -> None:
    """a line that must show up at the destination, sent through the new collector.

    the updater itself is labelled disco.log.core=true, so its own output reaches CORE
    destinations; for GLOBAL ones a throwaway container emits the line. the container
    is not `--rm`: an auto-removed container that exits at once is gone before the
    collector attaches to it.
    """
    from disco.config import BUSYBOX_VERSION
    from disco.utils import docker

    marker = f"disco logging migration to vector 0.34.0: {type} {url}"
    print(marker)
    if type != "GLOBAL":
        return
    name = f"disco-logging-migration-{docker.vectorconfig.destination_id(url, type)}"
    await call(["docker", "rm", "-f", name])
    await check_call(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            "disco.log.migration=true",
            f"busybox:{BUSYBOX_VERSION}",
            "sh",
            "-c",
            f"echo '{marker}'; sleep 5",
        ]
    )
    await asyncio.sleep(6)
    await call(["docker", "rm", "-f", name])


async def task_0_32_x(image: str) -> None:
    from disco.scripts.init import start_caddy
    from disco.utils import docker

    print("Updating from 0.32.x to 0.33.0")
    await alembic_upgrade("4c1f2a9e7b30")
    async with ReadSession.begin() as dbsession:
        host_home = await keyvalues.get_value(dbsession=dbsession, key="HOST_HOME")
        cloudflare_tunnel_token = await keyvalues.get_value(
            dbsession=dbsession, key="CLOUDFLARE_TUNNEL_TOKEN"
        )
    assert host_home is not None
    await _write_disco_files_of_live_deployments(host_home, image)
    await _write_pending_disco_files_of_queued_deployments(host_home, image)
    if await docker.container_exists("disco-caddy"):
        print("Removing the Caddy container")
        await docker.remove_container("disco-caddy")
    if not await docker.service_exists("disco-caddy"):
        print("Starting Caddy as a Swarm service")
        await start_caddy(
            host_home=host_home, tunnel=cloudflare_tunnel_token is not None
        )
    await _caddy_curl(
        host_home,
        image,
        "--request",
        "POST",
        "--header",
        "Content-Type: application/json",
        "http://disco-caddy/config/apps/tls",
        body=json.dumps(
            {
                "automation": {
                    "policies": [
                        {
                            "issuers": [
                                {"module": "acme"},
                                {
                                    "module": "acme",
                                    "ca": "https://acme.zerossl.com/v2/DV90",
                                    "email": "zerossl@disco.cloud",
                                },
                            ]
                        }
                    ]
                }
            }
        ),
    )
    print("tls automation policy installed (let's encrypt + zerossl fallback)")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.33.0"
        )


@dataclass
class LiveDiscoFile:
    """The disco file of a project's live deployment, to write in the project
    directory if it is not what is there."""

    project_name: str
    deployment_id: str
    disco_file: str
    # the deployed commit when the project has a repository: the disco file
    # is compared with the one of that commit rather than the working tree
    commit_hash: str | None


async def _write_disco_files_of_live_deployments(host_home: str, image: str) -> None:
    from disco.utils.deployments import get_live_deployment
    from disco.utils.projects import get_all_projects

    to_check: list[LiveDiscoFile] = []
    async with ReadSession.begin() as dbsession:
        for project in await get_all_projects(dbsession):
            deployment = await get_live_deployment(dbsession, project)
            if deployment is None or deployment.disco_file is None:
                continue
            has_repo = await project.awaitable_attrs.github_repo is not None
            commit_hash = deployment.commit_hash if has_repo else None
            if commit_hash is not None and not re.fullmatch(
                r"[0-9a-f]{40}", commit_hash
            ):
                print(
                    f"Not checking the disco file of {project.name}: its live "
                    f"deployment has an unexpected commit {commit_hash}"
                )
                continue
            env_var_names = [
                env_var.name
                for env_var in await deployment.awaitable_attrs.env_variables
            ]
            if "DISCO_JSON_PATH" in env_var_names:
                # would need Docker Swarm secret to decrypt env variable
                print(
                    f"Not checking the disco file of {project.name}: DISCO_JSON_PATH "
                    "is set and cannot be read during the update"
                )
                continue
            to_check.append(
                LiveDiscoFile(
                    project_name=project.name,
                    deployment_id=deployment.id,
                    disco_file=deployment.disco_file,
                    commit_hash=commit_hash,
                )
            )
    for live in to_check:
        try:
            await _write_disco_file_of_live_deployment(host_home, image, live)
        except Exception as ex:
            print(
                f"Could not set up the project directory of {live.project_name}: {ex}"
            )


async def _write_disco_file_of_live_deployment(
    host_home: str, image: str, live: LiveDiscoFile
) -> None:
    from disco.utils.deployments import get_deployment_by_id

    project_name = live.project_name
    disco_file = live.disco_file
    commit_hash = live.commit_hash
    if commit_hash is None:
        # no repository: the file in the project directory, if any
        deployed = await _read_project_file(
            host_home, image, project_name, "disco.json"
        )
    else:
        # a clone: the file at the live deployment's commit (the working tree
        # may be at the commit of a later deployment that did not complete)
        deployed = await _read_project_file(
            host_home, image, project_name, "disco.json", commit_hash
        )
        if deployed is None:
            print(
                f"Not checking the disco file of {project_name}: not found at "
                f"commit {commit_hash[:12]}"
            )
            return
    if deployed is not None and json.loads(deployed) == json.loads(disco_file):
        return
    print(
        f"The project directory of {project_name} becomes its live deployment's disco file"
    )
    # the update container does not mount the projects directory
    await check_call(
        [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--mount",
            f"type=bind,source={host_home}/disco/projects,target=/disco/projects",
            image,
            "sh",
            "-c",
            f"rm -rf /disco/projects/{project_name} "
            f"&& mkdir /disco/projects/{project_name} "
            f"&& cat > /disco/projects/{project_name}/disco.json",
        ],
        stdin=disco_file,
    )
    if commit_hash is not None:
        # deployed with a disco file posted to the API rather than from the
        # repository: a FILES deployment; without a commit, an env variable
        # change builds the project directory instead of checking it out
        async with Session.begin() as dbsession:
            deployment = await get_deployment_by_id(dbsession, live.deployment_id)
            assert deployment is not None
            deployment.deployment_type = "FILES"
            deployment.commit_hash = None


async def _write_pending_disco_files_of_queued_deployments(
    host_home: str, image: str
) -> None:
    from sqlalchemy import select

    from disco.models import Deployment
    from disco.utils.pendingfiles import pending_path

    async with ReadSession.begin() as dbsession:
        stmt = (
            select(Deployment)
            .where(Deployment.status == "QUEUED")
            .where(Deployment.disco_file.is_not(None))
        )
        queued = [
            (d.id, d.project_name, d.number, d.disco_file)
            for d in (await dbsession.execute(stmt)).scalars().all()
        ]
    for deployment_id, project_name, number, disco_file in queued:
        assert disco_file is not None
        print(
            f"Writing the disco file of the queued deployment {number} of "
            f"{project_name} as its pending files"
        )
        try:
            pending = pending_path(project_name, number)
            await check_call(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--interactive",
                    "--mount",
                    f"type=bind,source={host_home}/disco/projects,target=/disco/projects",
                    image,
                    "sh",
                    "-c",
                    f"rm -rf {pending} && mkdir {pending} && cat > {pending}/disco.json",
                ],
                stdin=disco_file,
            )
            async with Session.begin() as dbsession:
                deployment = await dbsession.get(Deployment, deployment_id)
                assert deployment is not None
                deployment.deployment_type = "FILES"
                deployment.commit_hash = None
        except Exception as ex:
            print(
                f"Could not write the pending files of deployment {number} of "
                f"{project_name}: {ex}"
            )


async def _read_project_file(
    host_home: str,
    image: str,
    project_name: str,
    path: str,
    commit_hash: str | None = None,
) -> str | None:
    """A file of the project directory, or of a commit of the clone there."""
    if commit_hash is None:
        command = f"cat /disco/projects/{project_name}/{path}"
    else:
        command = f"cd /disco/projects/{project_name} && git show {commit_hash}:{path}"
    stdout, _, process = await call(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={host_home}/disco/projects,target=/disco/projects",
            image,
            "sh",
            "-c",
            command,
        ]
    )
    if process.returncode != 0:
        return None
    return "\n".join(stdout)


async def task_0_31_x(image: str) -> None:
    print("Updating from 0.31.x to 0.32.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.32.0"
        )


async def task_0_30_x(image: str) -> None:
    print("Updating from 0.30.x to 0.31.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.31.0"
        )


async def task_0_29_x(image: str) -> None:
    print("Updating from 0.29.x to 0.30.0")
    await alembic_upgrade("d8adabff2804")
    async with Session.begin() as dbsession:
        registry = await keyvalues.get_value(dbsession, "REGISTRY_HOST")
        await keyvalues.set_value(dbsession=dbsession, key="REGISTRY", value=registry)
        await keyvalues.delete_value(dbsession=dbsession, key="REGISTRY_HOST")
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.30.0"
        )


async def task_0_28_x(image: str) -> None:
    print("Updating from 0.28.x to 0.29.0")
    async with ReadSession.begin() as dbsession:
        host_home = await keyvalues.get_value(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    caddy_config = await get_caddy_config(host_home, image)
    assert caddy_config is not None
    encode_handler = {"handler": "encode", "encodings": {"gzip": {}, "zstd": {}}}
    routes = caddy_config["apps"]["http"]["servers"]["disco"]["routes"]
    for route in routes:
        if "handle" not in route:
            continue
        for handler in route["handle"]:
            if handler.get("handler") != "subroute":
                continue
            for subroute in handler.get("routes", []):
                handles = subroute.get("handle", [])
                has_encode = any(h.get("handler") == "encode" for h in handles)
                if not has_encode and len(handles) > 0:
                    handles.insert(0, encode_handler)
    await set_caddy_config(host_home, image, caddy_config)
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.29.0"
        )


async def task_0_27_x(image: str) -> None:
    print("Updating from 0.27.x to 0.28.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.28.0"
        )


async def task_0_26_x(image: str) -> None:
    print("Updating from 0.26.x to 0.27.0")
    await alembic_upgrade("b0b4edb3672a")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.27.0"
        )


async def start_caddy_container(host_home: str, tunnel: bool) -> None:
    """Caddy as a plain container, how it ran before 0.33.0."""
    more_args = []
    if not tunnel:
        more_args += [
            "--publish",
            "published=80,target=80,protocol=tcp",
            "--publish",
            "published=443,target=443,protocol=tcp",
            "--publish",
            "published=443,target=443,protocol=udp",
        ]
    await run_and_print(
        [
            "docker",
            "run",
            "--name",
            "disco-caddy",
            "--detach",
            "--restart",
            "always",
            "--mount",
            "source=disco-caddy-data,target=/data",
            "--mount",
            "source=disco-caddy-config,target=/config",
            "--network",
            "disco-main",
            "--mount",
            f"type=bind,source={host_home}/disco/caddy-socket,target=/disco/caddy-socket",
            "--mount",
            "source=disco-caddy-init-config,target=/initconfig",
            "--mount",
            f"type=bind,source={host_home}/disco/srv,target=/disco/srv",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=20m",
            "--log-opt",
            "max-file=5",
            *more_args,
            f"caddy:{config.CADDY_VERSION}",
            "caddy",
            "run",
            "--resume",
            "--config",
            "/initconfig/config.json",
        ]
    )


async def task_0_25_x(image: str) -> None:
    from disco.utils import docker

    print("Updating from 0.25.x to 0.26.0")
    async with ReadSession.begin() as dbsession:
        host_home = await keyvalues.get_value_str(dbsession=dbsession, key="HOST_HOME")
        cloudflare_tunnel_token = await keyvalues.get_value(
            dbsession=dbsession, key="CLOUDFLARE_TUNNEL_TOKEN"
        )
    await run_and_print(
        [
            "docker",
            "container",
            "stop",
            "disco-caddy",
        ]
    )
    await run_and_print(
        [
            "docker",
            "container",
            "rm",
            "disco-caddy",
        ]
    )
    await start_caddy_container(
        host_home=host_home, tunnel=cloudflare_tunnel_token is not None
    )
    if cloudflare_tunnel_token is not None:
        await docker.add_network_to_container(
            "disco-caddy", "disco-cloudflare-tunnel", alias="disco-server"
        )
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.26.0"
        )


async def task_0_24_x(image: str) -> None:
    print("Updating from 0.24.x to 0.25.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.25.0"
        )


async def task_0_23_x(image: str) -> None:
    from disco.utils import docker
    from disco.utils.syslog import SyslogUrl, set_syslog_services

    print("Updating from 0.23.x to 0.24.0")
    async with Session.begin() as dbsession:
        disco_host = await keyvalues.get_value(dbsession, "DISCO_HOST")
        assert disco_host is not None
        urls_str = await keyvalues.get_value(dbsession, "SYSLOG_URLS")
        if urls_str is not None:
            urls = json.loads(urls_str)
            syslog_urls: list[SyslogUrl] = [
                {
                    "url": url,
                    "type": "GLOBAL",
                }
                for url in urls
            ]
            new_urls = json.dumps(syslog_urls)
            await keyvalues.set_value(dbsession, "SYSLOG_URLS", new_urls)
    if urls_str is not None:
        assert syslog_urls is not None
        await set_syslog_services(disco_host=disco_host, syslog_urls=syslog_urls)
    old_syslog_is_running = await docker.service_exists("disco-syslog")
    if old_syslog_is_running:
        await docker.rm_service("disco-syslog")

    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.24.0"
        )


async def task_0_22_x(image: str) -> None:
    from disco.utils import docker

    print("Updating from 0.22.x to 0.23.0")
    async with ReadSession.begin() as dbsession:
        host_home = await keyvalues.get_value_str(dbsession=dbsession, key="HOST_HOME")
        cloudflare_tunnel_token = await keyvalues.get_value(
            dbsession=dbsession, key="CLOUDFLARE_TUNNEL_TOKEN"
        )
    await run_and_print(["docker", "pull", f"caddy:{config.CADDY_VERSION}"])
    await run_and_print(
        [
            "docker",
            "container",
            "stop",
            "disco-caddy",
        ]
    )
    await run_and_print(
        [
            "docker",
            "container",
            "rm",
            "disco-caddy",
        ]
    )
    await start_caddy_container(
        host_home=host_home, tunnel=cloudflare_tunnel_token is not None
    )
    if cloudflare_tunnel_token is not None:
        await docker.add_network_to_container(
            "disco-caddy", "disco-cloudflare-tunnel", alias="disco-server"
        )
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.23.0"
        )


async def task_0_21_x(image: str) -> None:
    print("Updating from 0.21.x to 0.22.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.22.0"
        )


async def task_0_20_x(image: str) -> None:
    print("Updating from 0.20.x to 0.21.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.21.0"
        )


async def task_0_19_x(image: str) -> None:
    print("Updating from 0.19.x to 0.20.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.20.0"
        )


async def task_0_18_x(image: str) -> None:
    print("Updating from 0.18.x to 0.19.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.19.0"
        )


async def task_0_17_x(image: str) -> None:
    print("Updating from 0.17.x to 0.18.0")
    await alembic_upgrade("9087484963d4")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.18.0"
        )


async def task_0_16_x(image: str) -> None:
    print("Updating from 0.16.x to 0.17.0")
    async with ReadSession.begin() as dbsession:
        host_home = await keyvalues.get_value(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    caddy_config = await get_caddy_config(host_home, image)
    assert caddy_config is not None
    caddy_config["apps"]["http"]["servers"]["disco"]["logs"] = {}
    caddy_config["logging"] = {
        "logs": {
            "default": {
                "encoder": {
                    "fields": {
                        "request>headers": {"filter": "delete"},
                        "request>tls": {"filter": "delete"},
                        "resp_headers": {"filter": "delete"},
                        "user_id": {"filter": "delete"},
                    },
                    "format": "filter",
                    "wrap": {"format": "json"},
                }
            }
        }
    }
    await set_caddy_config(host_home, image, caddy_config)
    await alembic_upgrade("26877eda6774")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.17.0"
        )


async def task_0_15_x(image: str) -> None:
    print("Updating from 0.15.x to 0.16.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.16.0"
        )


async def task_0_14_x(image: str) -> None:
    print("Updating from 0.14.x to 0.15.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.15.0"
        )


async def task_0_13_x(image: str) -> None:
    print("Updating from 0.13.x to 0.14.0")
    await alembic_upgrade("b2c4ac1469de")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.14.0"
        )


async def task_0_12_x(image: str) -> None:
    print("Updating from 0.12.x to 0.13.0")
    async with ReadSession.begin() as dbsession:
        host_home = await keyvalues.get_value(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    await run_and_print(
        [
            "docker",
            "stop",
            "disco-caddy",
        ]
    )
    await run_and_print(
        [
            "docker",
            "rm",
            "disco-caddy",
        ]
    )
    await start_caddy_container(host_home=host_home, tunnel=False)
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.13.0"
        )


async def task_0_11_x(image: str) -> None:
    from disco.utils import docker

    print("Updating from 0.11.x to 0.12.0")
    await alembic_upgrade("b570b8c2424d")
    await docker.create_network("disco-main")
    services, _, _ = await check_call(
        [
            "docker",
            "service",
            "ls",
            "--filter",
            "label=disco.project.name",
            "--format",
            "{{ .Name }}",
        ]
    )
    for service in services:
        await run_and_print(
            [
                "docker",
                "service",
                "update",
                "--network-add",
                "disco-main",
                service,
            ]
        )
    networks, _, _ = await check_call(
        [
            "docker",
            "network",
            "ls",
            "--filter",
            "label=disco.project.name",
            "--format",
            "{{ .Name }}",
        ]
    )
    for network in networks:
        if not network.endswith("-caddy"):
            continue
        try:
            await docker.remove_network_from_container("disco-caddy", network)
        except Exception:
            log.info("Couldn't remove network %s from disco-caddy", network)
    await docker.add_network_to_container("disco-caddy", "disco-main")
    await docker.remove_network_from_container("disco-caddy", "disco-caddy-daemon")
    await docker.remove_network("disco-caddy-daemon")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.12.0"
        )


async def task_0_10_x(image: str) -> None:
    print("Updating from 0.10.x to 0.11.0")
    directory = "/disco/data/commandoutputs"

    def makedirs() -> None:
        if not os.path.isdir(directory):
            os.makedirs(directory)

    await asyncio.get_event_loop().run_in_executor(None, makedirs)
    async with ReadSession.begin() as dbsession:
        sql = """
            SELECT source
                FROM command_outputs
                GROUP BY source;
        """
        rows = (await dbsession.execute(text(sql))).all()
        sources = [row.source for row in rows]
    for source in sources:
        async with ReadSession.begin() as dbsession:
            db_url = (
                "sqlite+aiosqlite:////disco/data/commandoutputs/"
                f"{source.lower()}.sqlite3"
            )
            engine = create_async_engine(db_url)
            async with engine.begin() as output_conn:
                await output_conn.execute(
                    text("""
                    CREATE TABLE "command_outputs" (
                        id VARCHAR(32) NOT NULL, 
                        created DATETIME NOT NULL, 
                        text TEXT, 
                        CONSTRAINT pk_command_outputs PRIMARY KEY (id)
                    );
                    """)
                )
                await output_conn.execute(
                    text(
                        "CREATE INDEX ix_command_outputs_created "
                        "ON command_outputs (created);"
                    )
                )
                rows = (
                    await dbsession.execute(
                        text("""
                    SELECT id, created, text
                        FROM command_outputs
                        WHERE source = :source"""),
                        params={"source": source},
                    )
                ).all()
                for row in rows:
                    await output_conn.execute(
                        text("""
                    INSERT INTO command_outputs
                    (id, created, text) VALUES (:id, :created, :text)"""),
                        {"id": row.id, "created": row.created, "text": row.text},
                    )
            await engine.dispose()
    await alembic_upgrade("41a2f999a3e9")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.11.0"
        )


async def task_0_9_x(image: str) -> None:
    print("Updating from 0.9.x to 0.10.0")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.10.0"
        )


async def task_0_8_x(image: str) -> None:
    print("Updating from 0.8.x to 0.9.0")

    async with ReadSession.begin() as dbsession:
        host_home = await keyvalues.get_value(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    await run_and_print(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={host_home},target=/host-home",
            image,
            "mkdir",
            "/host-home/disco/caddy-socket",
        ]
    )
    await run_and_print(
        [
            "docker",
            "container",
            "stop",
            "disco-caddy",
        ]
    )
    await run_and_print(
        [
            "docker",
            "container",
            "rm",
            "disco-caddy",
        ]
    )
    await run_and_print(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            "source=disco-caddy-config,target=/disco/caddy/config",
            image,
            "sed",
            "-i",
            "s,var/run/caddy,disco/caddy-socket,g",
            "/disco/caddy/config/caddy/autosave.json",
        ]
    )
    await start_caddy_container(host_home=host_home, tunnel=False)
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.9.0"
        )


async def task_0_7_x(image: str) -> None:
    from disco.models import ProjectGithubRepo

    print("Updating from 0.7.x to 0.8.0")
    await alembic_upgrade("3fe4af6efa33")
    async with Session.begin() as dbsession:
        sql = """
            SELECT pgr.id, gar.full_name 
                FROM project_github_repos AS pgr 
                JOIN github_app_repos AS gar ON pgr.github_app_repo_id = gar.id;
        """
        rows = (await dbsession.execute(text(sql))).all()
        for row in rows:
            repo = await dbsession.get(ProjectGithubRepo, row.id)
            assert repo is not None
            repo.full_name = row.full_name
    async with Session.begin() as dbsession:
        await dbsession.execute(
            text("DELETE FROM project_github_repos WHERE full_name IS NULL")
        )
    await alembic_upgrade("7867432539d9")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value="0.8.0"
        )


async def task_patch(image: str) -> None:
    async with Session.begin() as dbsession:
        print(f"Updating to {disco.__version__}")
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value=disco.__version__
        )


def get_update_function_for_version(version: str) -> Callable[[str], Awaitable[None]]:
    if version.startswith("0.7."):
        return task_0_7_x
    if version.startswith("0.8."):
        return task_0_8_x
    if version.startswith("0.9."):
        return task_0_9_x
    if version.startswith("0.10."):
        return task_0_10_x
    if version.startswith("0.11."):
        return task_0_11_x
    if version.startswith("0.12."):
        return task_0_12_x
    if version.startswith("0.13."):
        return task_0_13_x
    if version.startswith("0.14."):
        return task_0_14_x
    if version.startswith("0.15."):
        return task_0_15_x
    if version.startswith("0.16."):
        return task_0_16_x
    if version.startswith("0.17."):
        return task_0_17_x
    if version.startswith("0.18."):
        return task_0_18_x
    if version.startswith("0.19."):
        return task_0_19_x
    if version.startswith("0.20."):
        return task_0_20_x
    if version.startswith("0.21."):
        return task_0_21_x
    if version.startswith("0.22."):
        return task_0_22_x
    if version.startswith("0.23."):
        return task_0_23_x
    if version.startswith("0.24."):
        return task_0_24_x
    if version.startswith("0.25."):
        return task_0_25_x
    if version.startswith("0.26."):
        return task_0_26_x
    if version.startswith("0.27."):
        return task_0_27_x
    if version.startswith("0.28."):
        return task_0_28_x
    if version.startswith("0.29."):
        return task_0_29_x
    if version.startswith("0.30."):
        return task_0_30_x
    if version.startswith("0.31."):
        return task_0_31_x
    if version.startswith("0.32."):
        return task_0_32_x
    if version.startswith("0.33."):
        return task_0_33_x
    if version.startswith("0.34."):
        assert disco.__version__.startswith("0.34.")
        return task_patch
    raise NotImplementedError(f"Updating from version {version} is not supported")


CADDY_SOCKET = "/disco/caddy-socket/caddy.sock"


async def _caddy_curl(
    host_home: str,
    image: str,
    *curl_args: str,
    body: str | None = None,
) -> str:
    """Call Caddy's admin API from a throwaway container of the new image.

    Because Caddy's socket is not mounted in the update script call.

    """
    curl = [
        "curl",
        "--silent",
        "--show-error",
        "--fail",
        "--retry",
        "30",
        "--retry-delay",
        "2",
        "--retry-all-errors",
        "--unix-socket",
        CADDY_SOCKET,
        *curl_args,
    ]
    if body is None:
        command = curl
    else:
        curl += ["--data-binary", "@/tmp/body.json"]
        command = ["sh", "-c", f"cat > /tmp/body.json && exec {shlex.join(curl)}"]
    stdout, _, _ = await check_call(
        [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--mount",
            f"type=bind,source={host_home}/disco/caddy-socket,target=/disco/caddy-socket",
            image,
            *command,
        ],
        stdin=body,
    )
    return "\n".join(stdout)


async def get_caddy_config(host_home: str, image: str) -> dict:
    return json.loads(await _caddy_curl(host_home, image, "http://disco-caddy/config/"))


async def set_caddy_config(host_home: str, image: str, config: dict) -> None:
    await _caddy_curl(
        host_home,
        image,
        "--request",
        "POST",
        "--header",
        "Content-Type: application/json",
        "http://disco-caddy/config/",
        body=json.dumps(config),
    )
