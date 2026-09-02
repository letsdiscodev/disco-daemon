"""Script that runs when updating Disco to the latest version"""

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import disco
from disco.models.db import ReadSession, Session, build_engines, get_engine
from disco.scripts.init import run_and_print, start_disco_daemon
from disco.utils import keyvalues
from disco.utils.meta import save_done_updating
from disco.utils.subprocess import check_call

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
        await task(image)
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
    get_caddy_config_cmd = (
        "from disco.utils import caddy; "
        "import json; "
        "print(json.dumps(caddy.get_config()))"
    )
    caddy_config_lines, _, _ = await check_call(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={host_home}/disco/caddy-socket,target=/disco/caddy-socket",
            image,
            "python",
            "-c",
            get_caddy_config_cmd,
        ]
    )
    caddy_config_str = "\n".join(caddy_config_lines)
    caddy_config = json.loads(caddy_config_str)
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
    caddy_config_str = json.dumps(caddy_config)
    set_caddy_config_cmd = (
        "from disco.utils import caddy; "
        "import json; "
        f"caddy_config_str = '''{caddy_config_str}''';"
        "caddy_config = json.loads(caddy_config_str);"
        "caddy.set_config(caddy_config)"
    )
    await run_and_print(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={host_home}/disco/caddy-socket,target=/disco/caddy-socket",
            image,
            "python",
            "-c",
            set_caddy_config_cmd,
        ]
    )
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


async def task_0_25_x(image: str) -> None:
    from disco.scripts.init import start_caddy
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
    await start_caddy(host_home=host_home, tunnel=cloudflare_tunnel_token is not None)
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
    from disco import config
    from disco.scripts.init import start_caddy
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
    await start_caddy(host_home=host_home, tunnel=cloudflare_tunnel_token is not None)
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
    get_caddy_config_cmd = (
        "from disco.utils import caddy; "
        "import json; "
        "print(json.dumps(caddy.get_config()))"
    )
    caddy_config_lines, _, _ = await check_call(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={host_home}/disco/caddy-socket,target=/disco/caddy-socket",
            image,
            "python",
            "-c",
            get_caddy_config_cmd,
        ]
    )
    caddy_config_str = "\n".join(caddy_config_lines)
    caddy_config = json.loads(caddy_config_str)
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
    caddy_config_str = json.dumps(caddy_config)
    set_caddy_config_cmd = (
        "from disco.utils import caddy; "
        "import json; "
        f"caddy_config_str = '''{caddy_config_str}''';"
        "caddy_config = json.loads(caddy_config_str);"
        "caddy.set_config(caddy_config)"
    )
    await run_and_print(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={host_home}/disco/caddy-socket,target=/disco/caddy-socket",
            image,
            "python",
            "-c",
            set_caddy_config_cmd,
        ]
    )
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
    from disco.scripts.init import start_caddy

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
    await start_caddy(host_home=host_home, tunnel=False)
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
    from disco.scripts.init import start_caddy

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
    await start_caddy(host_home=host_home, tunnel=False)
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
        assert disco.__version__.startswith("0.32.")
        return task_patch
    raise NotImplementedError(f"Updating from version {version} is not supported")
