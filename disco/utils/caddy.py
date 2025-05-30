import asyncio
import json
import socket
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool

from disco.utils.filesystem import static_site_deployment_path

HEADERS = {"Accept": "application/json"}
BASE_URL = "http://disco-caddy"


class CaddyConnection(HTTPConnection):
    def __init__(self):
        super().__init__("disco-caddy")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/disco/caddy-socket/caddy.sock")


class CaddyConnectionPool(HTTPConnectionPool):
    def __init__(self):
        super().__init__("disco-caddy")

    def _new_conn(self):
        return CaddyConnection()


class CaddyAdapter(HTTPAdapter):
    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        return CaddyConnectionPool()


def _get_session():
    session = requests.Session()
    session.mount("http://disco-caddy", CaddyAdapter())
    return session


def get_config() -> dict[str, Any] | None:
    session = _get_session()
    url = f"{BASE_URL}/config/"
    response = session.get(url, headers=HEADERS, timeout=10)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")
    return response.json()


def set_config(config: dict[str, Any]) -> None:
    session = _get_session()
    url = f"{BASE_URL}/config/"
    response = session.post(url, json=config, headers=HEADERS, timeout=10)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def _add_project_route(project_name: str, domains: list[str]) -> None:
    url = f"{BASE_URL}/config/apps/http/servers/disco/routes/0"
    req_body = {
        "@id": f"disco-project-{project_name}",
        "handle": [
            {
                "handler": "subroute",
                "routes": [
                    {
                        "handle": [
                            {
                                "@id": f"disco-project-handler-{project_name}",
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": "disco:80"}],
                            }
                        ]
                    },
                ],
            }
        ],
        "match": [{"@id": f"disco-project-hosts-{project_name}", "host": domains}],
        "terminal": True,
    }
    session = _get_session()

    def query() -> requests.Response:
        return session.put(url, json=req_body, headers=HEADERS, timeout=10)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def set_domains_for_project(project_name: str, domains: list[str]) -> None:
    if len(domains) == 0:
        if await _project_route_exists(project_name):
            await _remove_project_route(project_name)
    else:
        if await _project_route_exists(project_name):
            await _update_project_domains(project_name, domains)
        else:
            await _add_project_route(project_name, domains)


async def _remove_project_route(project_name: str) -> None:
    url = f"{BASE_URL}/id/disco-project-{project_name}"
    session = _get_session()

    def query() -> requests.Response:
        return session.delete(url, headers=HEADERS, timeout=10)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def _project_route_exists(project_name) -> bool:
    url = f"{BASE_URL}/id/disco-project-{project_name}"
    session = _get_session()

    def query() -> requests.Response:
        return session.get(url, headers=HEADERS, timeout=10)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    if response.status_code == 200:
        return True
    elif response.status_code == 404:
        return False
    else:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def _update_project_domains(project_name: str, domains: list[str]) -> None:
    url = f"{BASE_URL}/id/disco-project-hosts-{project_name}"
    req_body = {"@id": f"disco-project-hosts-{project_name}", "host": domains}
    session = _get_session()

    def query() -> requests.Response:
        return session.patch(url, json=req_body, headers=HEADERS, timeout=10)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


def serve_service_sync(project_name: str, container_name: str, port: int) -> None:
    url = f"{BASE_URL}/id/disco-project-handler-{project_name}"
    req_body = {
        "@id": f"disco-project-handler-{project_name}",
        "handler": "reverse_proxy",
        "upstreams": [{"dial": f"{container_name}:{port}"}],
    }
    session = _get_session()
    response = session.patch(url, json=req_body, headers=HEADERS, timeout=10)
    # TODO also accept 404? (when deploying project that has a web service and no domains set)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def serve_service(project_name: str, container_name: str, port: int) -> None:
    url = f"{BASE_URL}/id/disco-project-handler-{project_name}"
    req_body = {
        "@id": f"disco-project-handler-{project_name}",
        "handler": "reverse_proxy",
        "upstreams": [{"dial": f"{container_name}:{port}"}],
    }
    session = _get_session()

    def query() -> requests.Response:
        return session.patch(url, json=req_body, headers=HEADERS, timeout=10)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    # TODO also accept 404? (when deploying project that has a web service and no domains set)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def add_apex_www_redirects(
    domain_id: str, from_domain: str, to_domain: str
) -> None:
    url = f"{BASE_URL}/config/apps/http/servers/disco/routes/0"
    req_body = {
        "@id": f"apex-www-redirect-{domain_id}",
        "handle": [
            {
                "handler": "subroute",
                "routes": [
                    {
                        "handle": [
                            {
                                "handler": "static_response",
                                "headers": {
                                    "Location": [
                                        f"https://{to_domain}{{http.request.uri}}"
                                    ]
                                },
                                "status_code": 301,
                            }
                        ]
                    }
                ],
            }
        ],
        "match": [{"host": [from_domain]}],
        "terminal": True,
    }
    session = _get_session()

    def query() -> requests.Response:
        return session.put(url, json=req_body, headers=HEADERS, timeout=10)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def remove_apex_www_redirects(domain_id: str) -> None:
    url = f"{BASE_URL}/id/apex-www-redirect-{domain_id}"
    session = _get_session()

    def query() -> requests.Response:
        return session.delete(url, headers=HEADERS, timeout=10)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def get_served_service_for_project(project_name: str) -> str | None:
    url = f"{BASE_URL}/id/disco-project-handler-{project_name}/upstreams/0/dial"
    session = _get_session()

    def query() -> requests.Response:
        return session.get(url, headers=HEADERS, timeout=10)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    if response.status_code != 200:
        return None
    try:
        return response.json().split(":")[0]
    except Exception:
        return None


def serve_static_site_sync(project_name: str, deployment_number: int) -> None:
    url = f"{BASE_URL}/id/disco-project-handler-{project_name}"
    req_body = {
        "@id": f"disco-project-handler-{project_name}",
        "handler": "file_server",
        "root": static_site_deployment_path(project_name, deployment_number),
    }
    session = _get_session()
    response = session.patch(url, json=req_body, headers=HEADERS, timeout=10)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def serve_static_site(project_name: str, deployment_number: int) -> None:
    url = f"{BASE_URL}/id/disco-project-handler-{project_name}"
    req_body = {
        "@id": f"disco-project-handler-{project_name}",
        "handler": "file_server",
        "root": static_site_deployment_path(project_name, deployment_number),
    }
    session = _get_session()

    def query() -> requests.Response:
        return session.patch(url, json=req_body, headers=HEADERS, timeout=10)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def update_disco_host(disco_host: str) -> None:
    url = f"{BASE_URL}/id/disco-domain-handle/match/0/host/0"
    req_body = disco_host
    session = _get_session()

    def query() -> requests.Response:
        return session.patch(url, json=req_body, headers=HEADERS, timeout=10)

    response = await asyncio.get_event_loop().run_in_executor(None, query)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


def write_caddy_init_config(disco_host: str, tunnel: bool) -> None:
    # We write the initial config directly to the config file so that Caddy listens
    # to the unix socket instead of a regular port.
    # We use a unix socket because that's the only way at the moment to let only Disco
    # update the config using the endpoints. Otherwise, projects could read/write #
    # the config.
    init_config = {
        "admin": {
            "enforce_origin": False,
            "listen": "unix//disco/caddy-socket/caddy.sock",
            "origins": ["disco-caddy"],
        },
        "apps": {
            "http": {
                "servers": {
                    "disco": {
                        "listen": [":80" if tunnel else ":443"],
                        "routes": [
                            {
                                "@id": "disco-domain-handle",
                                "handle": [
                                    {
                                        "handler": "subroute",
                                        "routes": [
                                            {
                                                "handle": [
                                                    {
                                                        "@id": "domain-handle-disco-handle",
                                                        "handler": "reverse_proxy",
                                                        "upstreams": [
                                                            {"dial": "disco:80"}
                                                        ],
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                ],
                                "match": [{"host": [disco_host]}],
                                "terminal": True,
                            }
                        ],
                        "protocols": ["h1", "h2"],
                        "logs": {},
                    }
                }
            }
        },
        "logging": {
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
        },
    }
    with open("/initconfig/config.json", "w", encoding="utf-8") as f:
        json.dump(init_config, f)
