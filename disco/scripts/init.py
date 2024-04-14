"""Script that runs when installing Disco on a server"""

import logging
import os
import subprocess
from datetime import datetime, timedelta

from alembic import command
from alembic.config import Config

import disco
from disco import config
from disco.models.db import Session, engine
from disco.models.meta import metadata
from disco.utils import docker, keyvalues
from disco.utils.apikeys import create_api_key
from disco.utils.caddy import write_caddy_init_config
from disco.utils.encryption import generate_key

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    disco_ip = os.environ.get("DISCO_IP")
    host_home = os.environ.get("HOST_HOME")
    assert disco_ip is not None
    assert host_home is not None
    create_database()
    print("Setting initial state in internal database")
    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(
                dbsession=dbsession, key="DISCO_VERSION", value=disco.__version__
            )
            keyvalues.set_value(dbsession=dbsession, key="DISCO_IP", value=disco_ip)
            keyvalues.set_value(dbsession=dbsession, key="DISCO_HOST", value=disco_ip)
            keyvalues.set_value(dbsession=dbsession, key="HOST_HOME", value=host_home)
            keyvalues.set_value(dbsession=dbsession, key="REGISTRY_HOST", value=None)
            api_key = create_api_key(dbsession=dbsession, name="First API key")
            print("Created API key:", api_key.id)
    create_caddy_socket_dir()
    create_projects_dir(host_home)
    create_static_site_dir(host_home)
    print("Initializing Docker Swarm")
    create_docker_config(host_home)
    docker_swarm_init(disco_ip)
    node_id = get_this_swarm_node_id()
    label_swarm_node(node_id, "disco-role=main")
    docker.create_network("disco-caddy-daemon")
    docker.create_network("disco-logging")
    public_ca_cert = certificate_stuff(disco_ip)
    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(
                dbsession=dbsession, key="PUBLIC_CA_CERT", value=public_ca_cert
            )
    print(public_ca_cert)
    docker_swarm_create_disco_encryption_key()
    print("Setting up Caddy web server")
    write_caddy_init_config(disco_ip)
    start_caddy(host_home)
    print("Setting up Disco")
    start_disco_daemon(host_home)


def _run_cmd(args: list[str], timeout=600) -> str:
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    timeout_dt = datetime.utcnow() + timedelta(seconds=timeout)
    output = ""
    for line in process.stdout:
        decoded_line = line.decode("utf-8")
        output += decoded_line
        print(decoded_line, end="", flush=True)
        if datetime.utcnow() > timeout_dt:
            process.terminate()
            raise Exception(f"Running command failed, timeout after {timeout} seconds")
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}:\n{output}")
    print("", flush=True)
    return output


def create_database():
    print("Creating Disco internal database")
    metadata.create_all(engine)
    config = Config("/disco/app/alembic.ini")
    command.stamp(config, "head")


def docker_swarm_init(disco_ip: str) -> None:
    _run_cmd(
        [
            "docker",
            "swarm",
            "init",
            "--advertise-addr",
            disco_ip,
        ]
    )


def docker_swarm_create_disco_encryption_key() -> None:
    print("Generating encryption key for encryption at rest")
    process = subprocess.Popen(
        args=[
            "docker",
            "secret",
            "create",
            "disco_encryption_key",
            "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    stdout, _ = process.communicate(generate_key())
    print(stdout, flush=True)
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def get_this_swarm_node_id() -> str:
    output = _run_cmd(
        [
            "docker",
            "node",
            "inspect",
            "--format",
            "{{ .ID }}",
            "self",
        ]
    )
    node_id = output.replace("\n", "")
    return node_id


def label_swarm_node(node_id: str, label: str) -> None:
    _run_cmd(
        [
            "docker",
            "node",
            "update",
            "--label-add",
            label,
            node_id,
        ]
    )


def certificate_stuff(disco_ip: str) -> str:
    # TLS certificate for IP without domain
    # Create CA certificate, in a separate volume to hide from Caddy
    _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            "source=disco-cacerts,target=/cacerts",
            f"httpd:{config.HTTPD_VERSION}",
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:4096",
            "-nodes",
            "-keyout",
            "/cacerts/ca.key",
            "-out",
            "/cacerts/ca.crt",
            "-sha256",
            "-days",
            "36500",
            "-subj",
            f"/CN=Disco CA for {disco_ip}",
        ]
    )
    # Create certificate private key and signing request
    _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            "source=disco-certs,target=/certs",
            f"httpd:{config.HTTPD_VERSION}",
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:4096",
            "-keyout",
            f"/certs/{disco_ip}.key",
            "-out",
            f"/certs/{disco_ip}.csr",
            "-sha256",
            "-nodes",
            "-subj",
            f"/CN={disco_ip}",
            "-addext",
            f"subjectAltName=IP:{disco_ip}",
        ]
    )
    # Generate public key using signing request and CA certificate
    _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            "source=disco-cacerts,target=/cacerts",
            "--mount",
            "source=disco-certs,target=/certs",
            f"httpd:{config.HTTPD_VERSION}",
            "openssl",
            "x509",
            "-req",
            "-in",
            f"/certs/{disco_ip}.csr",
            "-CA",
            "/cacerts/ca.crt",
            "-CAkey",
            "/cacerts/ca.key",
            "-CAcreateserial",
            "-out",
            f"/certs/{disco_ip}.crt",
            "-days",
            "36500",
            "-copy_extensions",
            "copyall",
        ]
    )
    # Keep copy of CA public key in certs to expose later
    _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            "source=disco-cacerts,target=/cacerts",
            "--mount",
            "source=disco-certs,target=/certs",
            f"httpd:{config.HTTPD_VERSION}",
            "cp",
            "/cacerts/ca.crt",
            "/certs/ca.crt",
        ]
    )
    # Output CA public key, to be copied by CLI
    public_ca_cert = _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            "source=disco-cacerts,target=/cacerts",
            f"httpd:{config.HTTPD_VERSION}",
            "cat",
            "/cacerts/ca.crt",
        ]
    )
    return public_ca_cert


def create_caddy_socket_dir() -> None:
    os.makedirs("/host/var/run/caddy")


def start_caddy(host_home: str) -> None:
    _run_cmd(
        [
            "docker",
            "run",
            "--name",
            "disco-caddy",
            "--detach",
            "--restart",
            "always",
            "--publish",
            "published=80,target=80,protocol=tcp",
            "--publish",
            "published=443,target=443,protocol=tcp",
            "--publish",
            "published=443,target=443,protocol=udp",
            "--mount",
            "source=disco-certs,target=/certs",
            "--mount",
            "source=disco-caddy-data,target=/data",
            "--mount",
            "source=disco-caddy-config,target=/config",
            "--network",
            "disco-caddy-daemon",
            "--mount",
            "type=bind,source=/var/run/caddy,target=/var/run/caddy",
            "--mount",
            "source=disco-caddy-init-config,target=/initconfig",
            "--mount",
            f"type=bind,source={host_home}/disco/srv,target=/disco/srv",
            f"caddy:{config.CADDY_VERSION}",
            "caddy",
            "run",
            "--resume",
            "--config",
            "/initconfig/config.json",
        ]
    )


def create_projects_dir(host_home) -> None:
    os.makedirs(f"/host{host_home}/disco/projects")


def create_static_site_dir(host_home) -> None:
    os.makedirs(f"/host{host_home}/disco/srv")


def create_docker_config(host_home) -> None:
    # If the file doesn't exist, we create it so that we can mount it.
    # It's needed when we authenticate to a Docker Registry.
    path = f"/host{host_home}/.docker"
    if not os.path.isdir(path):
        os.makedirs(f"/host{host_home}/.docker")


def start_disco_daemon(host_home: str) -> None:
    _run_cmd(
        [
            "docker",
            "service",
            "create",
            "--name",
            "disco",
            "--network",
            "disco-caddy-daemon",
            "--network",
            "disco-logging",
            "--mount",
            "source=disco-data,target=/disco/data",
            "--mount",
            f"type=bind,source={host_home}/.ssh,target=/root/.ssh",
            "--mount",
            f"type=bind,source={host_home}/.docker,target=/root/.docker",
            "--mount",
            f"type=bind,source={host_home}/disco/projects,target=/disco/projects",
            "--mount",
            f"type=bind,source={host_home}/disco/srv,target=/disco/srv",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount",
            "type=bind,source=/var/run/caddy,target=/var/run/caddy",
            "--mount",
            "source=disco-certs,target=/certs",
            "--mount",
            "source=disco-caddy-data,target=/disco/caddy/data",
            "--mount",
            "source=disco-caddy-config,target=/disco/caddy/config",
            "--secret",
            "disco_encryption_key",
            "--constraint",
            "node.labels.disco-role==main",
            f"letsdiscodev/daemon:{disco.__version__}",
            "uvicorn",
            "disco.app:app",
            "--port",
            "80",
            "--host",
            "0.0.0.0",
            "--root-path",
            "/.disco",
        ]
    )
