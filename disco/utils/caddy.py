import asyncio
import json
import logging
import socket
import time
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool

from disco.utils import caddyconfig
from disco.utils.filesystem import static_site_deployment_path

log = logging.getLogger(__name__)

HEADERS = {"Accept": "application/json"}
BASE_URL = "http://disco-caddy"

# Edits go to the local Caddy; the config-sync module publishes them to dqlite.
# Wait until the local Caddy holds the published config before editing, or the
# sync module discards the edit. 412 means an If-Match conflict, retry.
MAX_EDIT_ATTEMPTS = 12
_GATE_TIMEOUT = 30.0
_GATE_POLL = 0.5


def _blocking_get_etag(session: requests.Session, path: str) -> str | None:
    response = session.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=10)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")
    return response.headers.get("Etag")


def _blocking_get_config_bytes(session: requests.Session) -> bytes | None:
    response = session.get(f"{BASE_URL}/config/", headers=HEADERS, timeout=10)
    if response.status_code != 200:
        return None
    return response.content


def _configs_equal(a: bytes | None, b: bytes | None) -> bool:
    if a is None or b is None:
        return False
    try:
        return json.loads(a) == json.loads(b)
    except ValueError:
        return a == b


async def _wait_until_current(
    timeout: float = _GATE_TIMEOUT, poll: float = _GATE_POLL
) -> None:
    loop = asyncio.get_event_loop()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        target = await caddyconfig.get_config_bytes()
        if target is None:
            return
        session = _get_session()
        local = await loop.run_in_executor(
            None, lambda: _blocking_get_config_bytes(session)
        )
        if _configs_equal(local, target):
            return
        await asyncio.sleep(poll)


async def _apply_edit(
    make_request: Callable[[requests.Session, dict], requests.Response],
    etag_path: str,
) -> requests.Response:
    loop = asyncio.get_event_loop()
    for _ in range(MAX_EDIT_ATTEMPTS):
        await _wait_until_current()
        session = _get_session()
        etag = await loop.run_in_executor(
            None, lambda: _blocking_get_etag(session, etag_path)
        )
        headers = dict(HEADERS)
        if etag is not None:
            headers["If-Match"] = etag
        response = await loop.run_in_executor(
            None, lambda: make_request(session, headers)
        )
        if response.status_code == 412:
            await asyncio.sleep(0.2)
            continue
        return response
    raise Exception("Caddy config edit did not converge (too many conflicts)")


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
    req_body: dict[str, Any] = {
        "@id": f"disco-project-{project_name}",
        "handle": [
            {
                "handler": "subroute",
                "routes": [
                    {
                        "handle": [
                            {
                                "handler": "encode",
                                "encodings": {"gzip": {}, "zstd": {}},
                            },
                            {
                                "@id": f"disco-project-handler-{project_name}",
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": "disco:80"}],
                            },
                        ]
                    },
                ],
            }
        ],
        "match": [{"@id": f"disco-project-hosts-{project_name}", "host": domains}],
        "terminal": True,
    }

    def make(session: requests.Session, headers: dict) -> requests.Response:
        return session.put(url, json=req_body, headers=headers, timeout=10)

    response = await _apply_edit(make, etag_path=url[len(BASE_URL):])
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

    def make(session: requests.Session, headers: dict) -> requests.Response:
        return session.delete(url, headers=headers, timeout=10)

    response = await _apply_edit(make, etag_path=url[len(BASE_URL):])
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

    def make(session: requests.Session, headers: dict) -> requests.Response:
        return session.patch(url, json=req_body, headers=headers, timeout=10)

    response = await _apply_edit(make, etag_path=url[len(BASE_URL):])
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
    req_body: dict[str, Any] = {
        "@id": f"disco-project-handler-{project_name}",
        "handler": "reverse_proxy",
        "upstreams": [{"dial": f"{container_name}:{port}"}],
    }

    def make(session: requests.Session, headers: dict) -> requests.Response:
        return session.patch(url, json=req_body, headers=headers, timeout=10)

    response = await _apply_edit(make, etag_path=url[len(BASE_URL):])
    # TODO also accept 404? (when deploying project that has a web service and no domains set)
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def add_apex_www_redirects(
    domain_id: str, from_domain: str, to_domain: str
) -> None:
    url = f"{BASE_URL}/config/apps/http/servers/disco/routes/0"
    req_body: dict[str, Any] = {
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

    def make(session: requests.Session, headers: dict) -> requests.Response:
        return session.put(url, json=req_body, headers=headers, timeout=10)

    response = await _apply_edit(make, etag_path=url[len(BASE_URL):])
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def remove_apex_www_redirects(domain_id: str) -> None:
    url = f"{BASE_URL}/id/apex-www-redirect-{domain_id}"

    def make(session: requests.Session, headers: dict) -> requests.Response:
        return session.delete(url, headers=headers, timeout=10)

    response = await _apply_edit(make, etag_path=url[len(BASE_URL):])
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

    def make(session: requests.Session, headers: dict) -> requests.Response:
        return session.patch(url, json=req_body, headers=headers, timeout=10)

    response = await _apply_edit(make, etag_path=url[len(BASE_URL):])
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


async def update_disco_host(disco_host: str) -> None:
    url = f"{BASE_URL}/id/disco-domain-handle/match/0/host/0"
    req_body = disco_host

    def make(session: requests.Session, headers: dict) -> requests.Response:
        return session.patch(url, json=req_body, headers=headers, timeout=10)

    response = await _apply_edit(make, etag_path=url[len(BASE_URL):])
    if response.status_code != 200:
        raise Exception(f"Caddy returned {response.status_code}: {response.text}")


def add_dqlite_config_fragments(config: dict[str, Any]) -> dict[str, Any]:
    config.setdefault("admin", {})["config"] = {"persist": False}
    config["storage"] = {"module": "dqlite"}
    config.setdefault("apps", {})["dqlite_config_sync"] = {
        "admin_endpoint": "unix//disco/caddy-socket/caddy.sock",
        "admin_origin": "disco-caddy",
    }
    return config


def build_caddy_base_config(disco_host: str, tunnel: bool) -> dict[str, Any]:
    return add_dqlite_config_fragments(_build_base_config(disco_host, tunnel))


def write_caddy_init_config(disco_host: str, tunnel: bool) -> None:
    # Written to the config file so Caddy listens on the unix socket instead of
    # a port; only Disco can then update the config, not projects.
    with open("/initconfig/config.json", "w", encoding="utf-8") as f:
        json.dump(_build_base_config(disco_host, tunnel), f)


def _build_base_config(disco_host: str, tunnel: bool) -> dict[str, Any]:
    return {
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
                                                        "handler": "encode",
                                                        "encodings": {
                                                            "gzip": {},
                                                            "zstd": {},
                                                        },
                                                    },
                                                    {
                                                        "@id": "domain-handle-disco-handle",
                                                        "handler": "reverse_proxy",
                                                        "upstreams": [
                                                            {"dial": "disco:80"}
                                                        ],
                                                    },
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


async def ensure_base_config(disco_host: str, tunnel: bool) -> bool:
    loop = asyncio.get_event_loop()
    session = _get_session()
    for _ in range(30):
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: session.get(
                    f"{BASE_URL}/id/disco-domain-handle", headers=HEADERS, timeout=10
                ),
            )
        except Exception:
            await asyncio.sleep(1)
            continue
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            break
        await asyncio.sleep(1)
    config = build_caddy_base_config(disco_host, tunnel)
    try:
        resp = await loop.run_in_executor(
            None,
            lambda: session.post(
                f"{BASE_URL}/config/", json=config, headers=HEADERS, timeout=10
            ),
        )
    except Exception as exc:
        log.error("Could not reach Caddy to seed base config: %s", exc)
        return False
    if resp.status_code != 200:
        log.error(
            "Caddy returned %s seeding base config: %s",
            resp.status_code,
            resp.text,
        )
        return False
    log.info("Seeded Caddy base config")
    return True
