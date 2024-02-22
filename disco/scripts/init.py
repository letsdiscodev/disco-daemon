"""Script that runs when installing Disco on a server"""
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta
from secrets import token_hex

from alembic import command
from alembic.config import Config

import disco
from disco import config
from disco.models.db import Session, engine
from disco.models.meta import metadata
from disco.utils import docker, keyvalues
from disco.utils.auth import create_api_key

log = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO)
    disco_ip = os.environ.get("DISCO_IP")
    host_home = os.environ.get("HOST_HOME")
    registry_username = token_hex(16)
    registry_password = token_hex(16)
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
            keyvalues.set_value(
                dbsession=dbsession, key="REGISTRY_HOST", value=disco_ip
            )
            keyvalues.set_value(
                dbsession=dbsession, key="REGISTRY_USERNAME", value=registry_username
            )
            keyvalues.set_value(
                dbsession=dbsession, key="REGISTRY_PASSWORD", value=registry_password
            )
            api_key = create_api_key(dbsession=dbsession, name="First API key")
            print("Created API key:", api_key.id)
    print("Initializing Docker Swarm")
    docker_swarm_init(disco_ip)
    node_id = get_this_swarm_node_id()
    label_swarm_node(node_id, "disco-role=main")
    docker.create_network("disco-caddy-registry", log_output=lambda x: None)
    docker.create_network("disco-caddy-daemon", log_output=lambda x: None)
    docker.create_network("disco-logging", log_output=lambda x: None)
    print("Setting up Docker Registry")
    add_creds_to_registry(registry_username, registry_password)
    start_docker_registry(disco_ip)
    public_ca_cert = certificate_stuff(disco_ip)
    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(
                dbsession=dbsession, key="PUBLIC_CA_CERT", value=public_ca_cert
            )
    print(public_ca_cert)
    print("Setting up Caddy web server")
    create_caddy_socket_dir()
    write_caddy_init_config(disco_ip)
    start_caddy()
    print("Setting up Disco")
    create_projects_dir(host_home)
    login_to_registry(
        host_home=host_home,
        host=disco_ip,
        username=registry_username,
        password=registry_password,
    )
    start_disco_daemon(host_home)
    start_disco_worker(host_home)


def _run_cmd(args: list[str], timeout=600) -> str:
    verbose = os.environ.get("DISCO_VERBOSE") == "true"
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
        if verbose:
            print(decoded_line, end="", flush=True)
        else:
            print(".", end="", flush=True)
        if datetime.utcnow() > timeout_dt:
            process.terminate()
            raise Exception(f"Running command failed, timeout after {timeout} seconds")
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}:\n{output}")
    if not verbose:
        print("", flush=True)
    return output


def create_database():
    print("Creating Disco internal database")
    metadata.create_all(engine)
    config = Config("/code/alembic.ini")
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


def add_creds_to_registry(username: str, password: str) -> None:
    _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "htpasswd",
            "--mount",
            "source=disco-registry-auth,target=/auth",
            f"httpd:{config.HTTPD_VERSION}",
            "-Bbc",
            "/auth/htpasswd",
            username,
            password,
        ]
    )


def start_docker_registry(host: str) -> None:
    _run_cmd(
        [
            "docker",
            "service",
            "create",
            "--name",
            "disco-registry",
            "--network",
            "disco-caddy-registry",
            "--mount",
            "source=disco-registry-auth,target=/auth",
            "--mount",
            "source=disco-registry-data,target=/var/lib/registry",
            "--env",
            f"REGISTRY_HTTP_HOST=https://{host}",
            "--env",
            "REGISTRY_AUTH=htpasswd",
            "--env",
            'REGISTRY_AUTH_HTPASSWD_REALM="Registry Realm"',
            "--env",
            "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd",
            "--constraint",
            "node.labels.disco-role==main",
            f"registry:{config.REGISTRY_VERSION}",
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
    os.makedirs(f"/host/etc/docker/certs.d/{disco_ip}")
    with open(
        f"/host/etc/docker/certs.d/{disco_ip}/ca.crt", "w", encoding="utf-8"
    ) as f:
        f.write(public_ca_cert)
    return public_ca_cert


def create_caddy_socket_dir() -> None:
    os.makedirs("/host/var/run/caddy")


def write_caddy_init_config(disco_ip) -> None:
    init_config = {
        "admin": {
            "enforce_origin": False,
            "listen": "unix//var/run/caddy/caddy.sock",
            "origins": ["disco-caddy"],
        },
        "apps": {
            "http": {
                "servers": {
                    "disco": {
                        "listen": [":443"],
                        "routes": [
                            {
                                "@id": "ip-handle",
                                "handle": [
                                    {
                                        "handler": "subroute",
                                        "routes": [
                                            {
                                                "match": [{"path": ["/.disco*"]}],
                                                "handle": [
                                                    {
                                                        "handler": "reverse_proxy",
                                                        "rewrite": {
                                                            "strip_path_prefix": "/.disco"
                                                        },
                                                        "upstreams": [
                                                            {
                                                                "dial": "disco-daemon:6543"
                                                            }
                                                        ],
                                                    }
                                                ],
                                            },
                                            {
                                                "handle": [
                                                    {
                                                        "handler": "reverse_proxy",
                                                        "upstreams": [
                                                            {
                                                                "dial": "disco-registry:5000"
                                                            }
                                                        ],
                                                    }
                                                ],
                                            },
                                        ],
                                    }
                                ],
                                "match": [{"host": [disco_ip]}],
                                "terminal": True,
                            }
                        ],
                        "tls_connection_policies": [{"fallback_sni": disco_ip}],
                    }
                }
            },
            "tls": {
                "certificates": {
                    "load_files": [
                        {
                            "certificate": f"/certs/{disco_ip}.crt",
                            "key": f"/certs/{disco_ip}.key",
                            "tags": ["cert0"],
                        }
                    ]
                }
            },
        },
    }
    with open("/initconfig/config.json", "w", encoding="utf-8") as f:
        json.dump(init_config, f)


def start_caddy() -> None:
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
            f"caddy:{config.CADDY_VERSION}",
            "caddy",
            "run",
            "--resume",
            "--config",
            "/initconfig/config.json",
        ]
    )
    docker.add_network_to_container(
        container="disco-caddy",
        network="disco-caddy-registry",
        log_output=lambda x: None,
    )


def create_projects_dir(host_home) -> None:
    os.makedirs(f"/host{host_home}/projects")


def login_to_registry(host_home, host, username, password) -> None:
    # use another container that has access to `/{host_home}/.docker`
    _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount",
            f"type=bind,source={host_home},target=/root",
            f"letsdiscodev/daemon:{disco.__version__}",
            "docker",
            "login",
            "--username",
            username,
            "--password",
            password,
            f"https://{host}",
        ]
    )


def start_disco_daemon(host_home: str) -> None:
    _run_cmd(
        [
            "docker",
            "service",
            "create",
            "--name",
            "disco-daemon",
            "--network",
            "disco-caddy-daemon",
            "--network",
            "disco-logging",
            "--mount",
            "source=disco-daemon-data,target=/code/data",
            "--mount",
            f"type=bind,source={host_home}/.ssh,target=/root/.ssh",
            "--mount",
            f"type=bind,source={host_home}/.docker/config.json,target=/root/.docker/config.json",
            "--mount",
            f"type=bind,source={host_home}/projects,target=/code/projects",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount",
            "type=bind,source=/var/run/caddy,target=/var/run/caddy",
            "--mount",
            "source=disco-certs,target=/certs",
            "--constraint",
            "node.labels.disco-role==main",
            "--with-registry-auth",
            f"letsdiscodev/daemon:{disco.__version__}",
            "uvicorn",
            "disco.app:app",
            "--port",
            "6543",
            "--host",
            "0.0.0.0",
            "--root-path",
            "/.disco",
        ]
    )


def start_disco_worker(host_home: str) -> None:
    _run_cmd(
        [
            "docker",
            "service",
            "create",
            "--name",
            "disco-worker",
            "--network",
            "disco-caddy-daemon",
            "--network",
            "disco-logging",
            "--mount",
            "source=disco-daemon-data,target=/code/data",
            "--mount",
            f"type=bind,source={host_home}/.ssh,target=/root/.ssh",
            "--mount",
            f"type=bind,source={host_home}/.docker/config.json,target=/root/.docker/config.json",
            "--mount",
            f"type=bind,source={host_home}/projects,target=/code/projects",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount",
            "type=bind,source=/var/run/caddy,target=/var/run/caddy",
            "--constraint",
            "node.labels.disco-role==main",
            "--with-registry-auth",
            f"letsdiscodev/daemon:{disco.__version__}",
            "disco_worker",
        ]
    )
