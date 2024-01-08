import requests
from disco.models import Project

HEADERS = {"Accept": "application/json"}
BASE_URL = "http://caddy:1900"


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
        ],
    )
    req_body["routes"][0]["@id"] = "disco-route"
    response = requests.post(url, json=req_body, headers=HEADERS)
    return response.status_code == 200


def add_project_route(project: Project) -> None:
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
        match=[dict(host=[project.domain])],
        terminal=True,
    )
    req_body["@id"] = project.name
    response = requests.post(url, json=req_body, headers=HEADERS)
    return response.status_code == 200


def serve_container(project: Project, container_name: str) -> None:
    url = f"{BASE_URL}/id/{project.name}"
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
        match=[dict(host=[project.domain])],
        terminal=True,
    )
    req_body["@id"] = project.name
    response = requests.patch(url, json=req_body, headers=HEADERS)
    return response.status_code == 200
