from typing import Any

import requests

HEADERS = {"Accept": "application/json"}
BASE_URL = "http://disco-caddy:1900"


def init_config(disco_ip: str) -> None:
    url = f"{BASE_URL}/config/"
    req_body: dict[str, Any] = {
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
        }
    }
    response = requests.post(url, json=req_body, headers=HEADERS, timeout=10)
    if response.status_code != 200:
        raise Exception("Caddy returned {response.status_code}: {response.text}")


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
    response = requests.put(url, json=req_body, headers=HEADERS, timeout=10)
    if response.status_code != 200:
        raise Exception("Caddy returned {response.status_code}: {response.text}")


def remove_project_route(project_name: str) -> None:
    url = f"{BASE_URL}/id/disco-project-{project_name}"
    response = requests.delete(url, headers=HEADERS, timeout=10)
    if response.status_code != 200:
        raise Exception("Caddy returned {response.status_code}: {response.text}")


def serve_service(project_name: str, container_name: str, port: int) -> None:
    url = f"{BASE_URL}/id/disco-project-handler-{project_name}"
    req_body = {
        "@id": f"disco-project-handler-{project_name}",
        "handler": "reverse_proxy",
        "upstreams": [{"dial": f"{container_name}:{port}"}],
    }
    response = requests.patch(url, json=req_body, headers=HEADERS, timeout=10)
    if response.status_code != 200:
        raise Exception("Caddy returned {response.status_code}: {response.text}")
