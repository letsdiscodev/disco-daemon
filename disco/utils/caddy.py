from typing import Any

import requests

HEADERS = {"Accept": "application/json"}
BASE_URL = "http://caddy:1900"


def set_empty_config() -> bool:
    url = f"{BASE_URL}/config/"
    req_body: dict[str, Any] = dict(apps=dict(http=dict(servers=dict())))
    response = requests.post(url, json=req_body, headers=HEADERS)
    return response.status_code == 200


def add_disco_domain(domain: str) -> bool:
    url = f"{BASE_URL}/config/apps/http/servers/disco"
    req_body = dict(
        listen=[":443"],
        routes=[
            dict(
                handle=[
                    dict(
                        handler="subroute",
                        routes=[
                            dict(
                                match=[dict(path=["/v2/*"])],
                                handle=[
                                    dict(
                                        handler="reverse_proxy",
                                        upstreams=[dict(dial="disco-registry:5000")],
                                    )
                                ],
                            ),
                            dict(
                                handle=[
                                    dict(
                                        handler="reverse_proxy",
                                        upstreams=[dict(dial="disco-daemon:6543")],
                                    )
                                ]
                            ),
                        ],
                    )
                ],
                match=[dict(host=[domain])],
                terminal=True,
            )
        ],
    )
    req_body["routes"][0]["@id"] = "disco-route"
    response = requests.post(url, json=req_body, headers=HEADERS)
    return response.status_code == 200


def add_project_route(project_id: str, domain: str) -> bool:
    url = f"{BASE_URL}/config/apps/http/servers/disco/routes"
    req_body = dict(
        handle=[
            dict(
                handler="subroute",
                routes=[
                    dict(
                        handle=[
                            dict(
                                handler="reverse_proxy",
                                upstreams=[dict(dial="disco-daemon:6543")],
                            )
                        ]
                    )
                ],
            )
        ],
        match=[dict(host=[domain])],
        terminal=True,
    )
    req_body["@id"] = f"disco-{project_id}"
    response = requests.post(url, json=req_body, headers=HEADERS)
    return response.status_code == 200


def serve_service(project_id: str, project_domain: str, container_name: str) -> bool:
    url = f"{BASE_URL}/id/disco-{project_id}"
    req_body = dict(
        handle=[
            dict(
                handler="subroute",
                routes=[
                    dict(
                        handle=[
                            dict(
                                handler="reverse_proxy",
                                upstreams=[dict(dial=f"{container_name}:8000")],
                            )
                        ]
                    )
                ],
            )
        ],
        match=[dict(host=[project_domain])],
        terminal=True,
    )
    req_body["@id"] = f"disco-{project_id}"
    response = requests.patch(url, json=req_body, headers=HEADERS)
    return response.status_code == 200
