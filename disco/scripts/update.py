"""Script that runs when updating Disco to the latest version"""

import json
import logging
import os
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

import disco
from disco.models.db import Session
from disco.scripts.init import start_disco_daemon
from disco.utils import keyvalues
from disco.utils.meta import save_done_updating

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    image = os.environ.get("DISCO_IMAGE")
    if image is None:  # backward compat for version <= 0.4.1
        image = "letsdiscodev/daemon:latest"
    with Session.begin() as dbsession:
        installed_version = keyvalues.get_value_sync(
            dbsession=dbsession, key="DISCO_VERSION"
        )
        assert installed_version is not None
    if installed_version == disco.__version__:
        print(f"Current version is latest ({disco.__version__}), not updating.")
        with Session.begin() as dbsession:
            save_done_updating(dbsession)
        return
    version_parts = installed_version.split(".")
    major = int(version_parts[0])
    minor = int(version_parts[1])
    if major == 0 and minor <= 4:
        with Session.begin() as dbsession:
            disco_host = keyvalues.get_value_sync(dbsession, "DISCO_HOST")
            disco_ip = keyvalues.get_value_sync(dbsession, "DISCO_IP")
            if disco_host == disco_ip:
                print("Must set Disco host first, not updating.")
                save_done_updating(dbsession)
                return
    print(f"Installed version: {installed_version}")
    print(f"New version: {disco.__version__}")
    print("Stopping existing Disco processes")
    try:
        stop_disco_daemon()
    except Exception:
        log.info("Failed to stop Disco")
    if re.match(r"^0\.(1|2|3)\..+$", installed_version):
        try:
            stop_disco_worker()
        except Exception:
            log.info("Failed to stop Disco Worker")
    print("Running upgrade tasks")
    ttl = 9999
    while installed_version != disco.__version__:
        assert installed_version is not None
        task = get_update_function_for_version(installed_version)
        task(image)
        with Session.begin() as dbsession:
            installed_version = keyvalues.get_value_sync(
                dbsession=dbsession, key="DISCO_VERSION"
            )
        ttl -= 1
        if ttl < 0:
            print(
                f"Caught in an infinite loop while upgrading from {installed_version}"
            )
            break

    print("Starting new version of Disco")
    with Session.begin() as dbsession:
        host_home = keyvalues.get_value_sync(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    start_disco_daemon(host_home, image)
    with Session.begin() as dbsession:
        save_done_updating(dbsession)


def _run_cmd(args: list[str], timeout=600) -> str:
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    timeout_dt = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    output = ""
    for line in process.stdout:
        decoded_line = line.decode("utf-8")
        output += decoded_line
        print(decoded_line, end="", flush=True)
        if datetime.now(timezone.utc) > timeout_dt:
            process.terminate()
            raise Exception(f"Running command failed, timeout after {timeout} seconds")
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}:\n{output}")
    print("", flush=True)
    return output


def stop_disco_daemon() -> None:
    _run_cmd(
        [
            "docker",
            "service",
            "rm",
            "disco",
        ]
    )


def stop_disco_worker() -> None:
    _run_cmd(
        [
            "docker",
            "service",
            "rm",
            "disco-worker",
        ]
    )


def alembic_upgrade(version_hash: str) -> None:
    config = Config("/disco/app/alembic.ini")
    command.upgrade(config, version_hash)


def task_0_20_x(image: str) -> None:
    print("Updating from 0.20.x to 0.21.0")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.21.0"
        )


def task_0_19_x(image: str) -> None:
    print("Updating from 0.19.x to 0.20.0")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.20.0"
        )


def task_0_18_x(image: str) -> None:
    print("Updating from 0.18.x to 0.19.0")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.19.0"
        )


def task_0_17_x(image: str) -> None:
    print("Updating from 0.17.x to 0.18.0")
    alembic_upgrade("9087484963d4")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.18.0"
        )


def task_0_16_x(image: str) -> None:
    print("Updating from 0.16.x to 0.17.0")
    with Session.begin() as dbsession:
        host_home = keyvalues.get_value_sync(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    get_caddy_config_cmd = (
        "from disco.utils import caddy; "
        "import json; "
        "print(json.dumps(caddy.get_config()))"
    )
    caddy_config_str = _run_cmd(
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
    _run_cmd(
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
    alembic_upgrade("26877eda6774")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.17.0"
        )


def task_0_15_x(image: str) -> None:
    print("Updating from 0.15.x to 0.16.0")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.16.0"
        )


def task_0_14_x(image: str) -> None:
    print("Updating from 0.14.x to 0.15.0")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.15.0"
        )


def task_0_13_x(image: str) -> None:
    print("Updating from 0.13.x to 0.14.0")
    alembic_upgrade("b2c4ac1469de")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.14.0"
        )


def task_0_12_x(image: str) -> None:
    from disco.scripts.init import start_caddy

    print("Updating from 0.12.x to 0.13.0")
    with Session.begin() as dbsession:
        host_home = keyvalues.get_value_sync(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    _run_cmd(
        [
            "docker",
            "stop",
            "disco-caddy",
        ]
    )
    _run_cmd(
        [
            "docker",
            "rm",
            "disco-caddy",
        ]
    )
    start_caddy(host_home=host_home, tunnel=False)
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.13.0"
        )


def task_0_11_x(image: str) -> None:
    from disco.utils import docker

    print("Updating from 0.11.x to 0.12.0")
    alembic_upgrade("b570b8c2424d")
    docker.create_network_sync("disco-main")
    services_output = _run_cmd(
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
    services = services_output.split("\n")[:-1]
    for service in services:
        _run_cmd(
            [
                "docker",
                "service",
                "update",
                "--network-add",
                "disco-main",
                service,
            ]
        )
    networks_output = _run_cmd(
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
    networks = networks_output.split("\n")[:-1]
    for network in networks:
        if not network.endswith("-caddy"):
            continue
        try:
            docker.remove_network_from_container("disco-caddy", network)
        except Exception:
            log.info("Couldn't remove network %s from disco-caddy", network)
    docker.add_network_to_container("disco-caddy", "disco-main")
    docker.remove_network_from_container("disco-caddy", "disco-caddy-daemon")
    docker.remove_network_sync("disco-caddy-daemon")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.12.0"
        )


def task_0_10_x(image: str) -> None:
    print("Updating from 0.10.x to 0.11.0")
    directory = "/disco/data/commandoutputs"
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with Session.begin() as dbsession:
        sql = """
            SELECT source
                FROM command_outputs
                GROUP BY source;
        """
        rows = dbsession.execute(text(sql)).all()
        sources = [row.source for row in rows]
    for source in sources:
        with Session.begin() as dbsession:
            db_url = f"sqlite:////disco/data/commandoutputs/{source.lower()}.sqlite3"
            engine = create_engine(db_url, connect_args={"check_same_thread": False})
            session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            with session.begin() as output_dbsession:
                output_dbsession.execute(
                    text("""
                    CREATE TABLE "command_outputs" (
                        id VARCHAR(32) NOT NULL, 
                        created DATETIME NOT NULL, 
                        text TEXT, 
                        CONSTRAINT pk_command_outputs PRIMARY KEY (id)
                    );
                    """)
                )
                output_dbsession.execute(
                    text(
                        "CREATE INDEX ix_command_outputs_created "
                        "ON command_outputs (created);"
                    )
                )
                rows = dbsession.execute(
                    text("""
                    SELECT id, created, text
                        FROM command_outputs
                        WHERE source = :source"""),
                    params={"source": source},
                ).all()
                for row in rows:
                    output_dbsession.execute(
                        text("""
                    INSERT INTO command_outputs
                    (id, created, text) VALUES (:id, :created, :text)"""),
                        params={"id": row.id, "created": row.created, "text": row.text},
                    )
    alembic_upgrade("41a2f999a3e9")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.11.0"
        )


def task_0_9_x(image: str) -> None:
    print("Updating from 0.9.x to 0.10.0")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.10.0"
        )


def task_0_8_x(image: str) -> None:
    print("Updating from 0.8.x to 0.9.0")
    from disco.scripts.init import start_caddy

    with Session.begin() as dbsession:
        host_home = keyvalues.get_value_sync(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    _run_cmd(
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
    _run_cmd(
        [
            "docker",
            "container",
            "stop",
            "disco-caddy",
        ]
    )
    _run_cmd(
        [
            "docker",
            "container",
            "rm",
            "disco-caddy",
        ]
    )
    _run_cmd(
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
    start_caddy(host_home=host_home, tunnel=False)
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.9.0"
        )


def task_0_7_x(image: str) -> None:
    from disco.models import ProjectGithubRepo

    print("Updating from 0.7.x to 0.8.0")
    alembic_upgrade("3fe4af6efa33")
    with Session.begin() as dbsession:
        sql = """
            SELECT pgr.id, gar.full_name 
                FROM project_github_repos AS pgr 
                JOIN github_app_repos AS gar ON pgr.github_app_repo_id = gar.id;
        """
        rows = dbsession.execute(text(sql)).all()
        for row in rows:
            repo = dbsession.get(ProjectGithubRepo, row.id)
            assert repo is not None
            repo.full_name = row.full_name
    with Session.begin() as dbsession:
        dbsession.execute(
            text("DELETE FROM project_github_repos WHERE full_name IS NULL")
        )
    alembic_upgrade("7867432539d9")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.8.0"
        )


def task_0_6_x(image: str) -> None:
    from disco.models import GithubApp, ProjectDomain
    from disco.utils.projectdomains import _get_apex_www_redirect_for_domain

    print("Updating from 0.6.x to 0.7.0")
    alembic_upgrade("97e98737cba8")
    with Session.begin() as dbsession:
        rows = dbsession.execute(text("SELECT id, domain FROM projects;")).all()
        all_domains = set([row.domain for row in rows if row.domain is not None])
        for row in rows:
            if row.domain is not None:
                project_domain = ProjectDomain(
                    id=uuid.uuid4().hex,
                    project_id=row.id,
                    name=row.domain,
                )
                dbsession.add(project_domain)
                apex_www_redirect = _get_apex_www_redirect_for_domain(row.domain)
                if (
                    apex_www_redirect is not None
                    and apex_www_redirect not in all_domains
                ):
                    add_redirect_cmd = (
                        "from disco.scripts.update import _add_apex_www_redirects; "
                        "_add_apex_www_redirects("
                        f"'{project_domain.id}', '{apex_www_redirect}', "
                        f"'{project_domain.name}')"
                    )
                    _run_cmd(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "--mount",
                            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
                            "type=bind,source=/var/run/caddy,target=/var/run/caddy",
                            image,
                            "python",
                            "-c",
                            add_redirect_cmd,
                        ]
                    )

        stmt = select(GithubApp).order_by(GithubApp.owner_login)
        github_apps = dbsession.execute(stmt).scalars().all()
        for github_app in github_apps:
            app_meta = json.loads(github_app.app_info)
            github_app.owner_id = app_meta["owner"]["id"]
            github_app.owner_login = app_meta["owner"]["login"]
            github_app.owner_type = app_meta["owner"]["type"]
    alembic_upgrade("47da35039f6f")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.7.0"
        )


def task_0_5_x(image: str) -> None:
    print("Updating from 0.5.x to 0.6.0")
    alembic_upgrade("5540c20f9acd")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.6.0"
        )


def task_0_4_x(image: str) -> None:
    print("Updating from 0.4.x to 0.5.0")
    alembic_upgrade("87c62632dfd1")
    with Session.begin() as dbsession:
        disco_ip = keyvalues.get_value_sync(dbsession=dbsession, key="DISCO_IP")
        get_caddy_config_cmd = (
            "from disco.utils import caddy; "
            "import json; "
            "print(json.dumps(caddy.get_config()))"
        )
        caddy_config_str = _run_cmd(
            [
                "docker",
                "run",
                "--rm",
                "--mount",
                "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
                "--mount",
                "type=bind,source=/var/run/caddy,target=/var/run/caddy",
                image,
                "python",
                "-c",
                get_caddy_config_cmd,
            ]
        )
        caddy_config = json.loads(caddy_config_str)
        assert caddy_config is not None
        del caddy_config["apps"]["http"]["servers"]["disco"]["tls_connection_policies"]
        del caddy_config["apps"]["tls"]
        caddy_config["apps"]["http"]["servers"]["disco"]["routes"] = [
            route
            for route in caddy_config["apps"]["http"]["servers"]["disco"]["routes"]
            if route.get("@id") != "ip-handle"
        ]
        caddy_config_str = json.dumps(caddy_config)
        set_caddy_config_cmd = (
            "from disco.utils import caddy; "
            "import json; "
            f"caddy_config_str = '''{caddy_config_str}''';"
            "caddy_config = json.loads(caddy_config_str);"
            "caddy.set_config(caddy_config)"
        )
        _run_cmd(
            [
                "docker",
                "run",
                "--rm",
                "--mount",
                "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
                "--mount",
                "type=bind,source=/var/run/caddy,target=/var/run/caddy",
                image,
                "python",
                "-c",
                set_caddy_config_cmd,
            ]
        )
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_ADVERTISE_ADDR", value=disco_ip
        )
        keyvalues.delete_value_sync(dbsession=dbsession, key="DISCO_IP")
        keyvalues.delete_value_sync(dbsession=dbsession, key="PUBLIC_CA_CERT")
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.5.0"
        )


def task_0_3_x(image: str) -> None:
    print("Updating from 0.3.x to 0.4.0")
    alembic_upgrade("3eb8871ccb85")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.4.0"
        )


def task_0_2_x(image: str) -> None:
    print("Updating from 0.2.x to 0.3.0")
    alembic_upgrade("d0cba3cd3238")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.3.0"
        )


def task_0_1_x(image: str) -> None:
    print("Updating from 0.1.x to 0.2.0")
    alembic_upgrade("eba27af20db2")
    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value="0.2.0"
        )


def task_patch(image: str) -> None:
    with Session.begin() as dbsession:
        print(f"Updating to {disco.__version__}")
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value=disco.__version__
        )


def get_update_function_for_version(version: str) -> Callable[[str], None]:
    if version.startswith("0.1."):
        return task_0_1_x
    if version.startswith("0.2."):
        return task_0_2_x
    if version.startswith("0.3."):
        return task_0_3_x
    if version.startswith("0.4."):
        return task_0_4_x
    if version.startswith("0.5."):
        return task_0_5_x
    if version.startswith("0.6."):
        return task_0_6_x
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
        assert disco.__version__.startswith("0.21.")
        return task_patch
    raise NotImplementedError(f"Update missing for version {version}")
