import socket

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool

HEADERS = {"Accept": "application/json"}
BASE_URL = "http://disco-caddy"


class CaddyConnection(HTTPConnection):
    def __init__(self):
        super().__init__("disco-caddy")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/caddy/caddy.sock")


class CaddyConnectionPool(HTTPConnectionPool):
    def __init__(self):
        super().__init__("disco-caddy")

    def _new_conn(self):
        return CaddyConnection()


class CaddyAdapter(HTTPAdapter):
    def get_connection(self, url, proxies=None):
        return CaddyConnectionPool()


def _get_session():
    session = requests.Session()
    session.mount("http://disco-caddy", CaddyAdapter())
    return session


def add_project_route(project_name: str, domain: str) -> None:
    url = f"{BASE_URL}/config/apps/http/servers/disco/routes/0"
    req_body = {
        "@id": f"disco-project-{project_name}",
        "handle": [
            {
                "handler": "subroute",
                "routes": [
                    {
                        "match": [{"path": ["/.disco*"]}],
                        "handle": [
                            {
                                "handler": "reverse_proxy",
                                "rewrite": {"strip_path_prefix": "/.disco"},
                                "upstreams": [{"dial": "disco-daemon:6543"}],
                            }
                        ],
                    },
                    {
                        "handle": [
                            {
                                "@id": f"disco-project-handler-{project_name}",
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": "disco-daemon:6543"}],
                            }
                        ]
                    },
                ],
            }
        ],
        "match": [{"@id": f"disco-project-hosts-{project_name}", "host": [domain]}],
        "terminal": True,
    }
    session = _get_session()
    response = session.put(url, json=req_body, headers=HEADERS, timeout=10)
    if response.status_code != 200:
        raise Exception("Caddy returned {response.status_code}: {response.text}")


def remove_project_route(project_name: str) -> None:
    url = f"{BASE_URL}/id/disco-project-{project_name}"
    session = _get_session()
    response = session.delete(url, headers=HEADERS, timeout=10)
    if response.status_code != 200:
        raise Exception("Caddy returned {response.status_code}: {response.text}")


def serve_service(project_name: str, container_name: str, port: int) -> None:
    url = f"{BASE_URL}/id/disco-project-handler-{project_name}"
    req_body = {
        "@id": f"disco-project-handler-{project_name}",
        "handler": "reverse_proxy",
        "upstreams": [{"dial": f"{container_name}:{port}"}],
    }
    session = _get_session()
    response = session.patch(url, json=req_body, headers=HEADERS, timeout=10)
    if response.status_code != 200:
        raise Exception("Caddy returned {response.status_code}: {response.text}")
