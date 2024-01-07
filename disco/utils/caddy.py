from typing import Any

import requests

HEADERS = {"Accept": "application/json"}
BASE_URL = "http://caddy:1900"


def get_current_config() -> dict[str, Any]:
    response = requests.get(f"{BASE_URL}/config/", headers=HEADERS)
    config = response.json()
    if config is None:
        return dict(
            apps=dict(
                http=dict(
                    servers=dict(),
                ),
            )
        )
    return config


def set_disco_domain(domain: str) -> bool:
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

    response = requests.post(url, json=req_body, headers=HEADERS)
    return response.status_code == 200
