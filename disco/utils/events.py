from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from disco.utils.deployments import DEPLOYMENT_STATUS
import logging

log = logging.getLogger(__name__)


@dataclass
class DiscoEvent:
    id: str
    timestamp: datetime
    type: str
    data: dict[str, Any]


_subscribers: list[asyncio.Queue[DiscoEvent]] = []
_events: list[DiscoEvent] = []


def deployment_created(
    project_name: str, deployment_number: int, status: DEPLOYMENT_STATUS
) -> None:
    _publish_event(
        _event(
            type="deployment:created",
            data={
                "project": {
                    "name": project_name,
                },
                "deployment": {
                    "number": deployment_number,
                    "status": status,
                },
            },
        )
    )


def deployment_status(
    project_name: str, deployment_number: int, status: DEPLOYMENT_STATUS
) -> None:
    _publish_event(
        _event(
            type="deployment:status",
            data={
                "project": {
                    "name": project_name,
                },
                "deployment": {
                    "number": deployment_number,
                    "status": status,
                },
            },
        )
    )


def env_variable_created(project_name: str, env_var: str) -> None:
    _publish_event(
        _event(
            type="envVar:created",
            data={
                "project": {
                    "name": project_name,
                },
                "envVar": {
                    "name": env_var,
                },
            },
        )
    )


def env_variable_updated(project_name: str, env_var: str) -> None:
    _publish_event(
        _event(
            type="envVar:updated",
            data={
                "project": {
                    "name": project_name,
                },
                "envVar": {
                    "name": env_var,
                },
            },
        )
    )


def env_variable_removed(project_name: str, env_var: str) -> None:
    _publish_event(
        _event(
            type="envVar:removed",
            data={
                "project": {
                    "name": project_name,
                },
                "envVar": {
                    "name": env_var,
                },
            },
        )
    )


def project_created(project_name: str) -> None:
    _publish_event(
        _event(
            type="project:created",
            data={
                "project": {
                    "name": project_name,
                },
            },
        )
    )


def project_removed(project_name: str) -> None:
    _publish_event(
        _event(
            type="project:removed",
            data={
                "project": {
                    "name": project_name,
                },
            },
        )
    )


def domain_created(project_name: str, domain: str) -> None:
    _publish_event(
        _event(
            type="domain:created",
            data={
                "project": {
                    "name": project_name,
                },
                "domain": {"name": domain},
            },
        )
    )


def domain_removed(project_name: str, domain: str) -> None:
    _publish_event(
        _event(
            type="domain:removed",
            data={
                "project": {
                    "name": project_name,
                },
                "domain": {"name": domain},
            },
        )
    )


def api_key_created(public_key: str, name: str) -> None:
    _publish_event(
        _event(
            type="apiKey:created",
            data={
                "apiKey": {
                    "publicKey": public_key,
                    "name": name,
                },
            },
        )
    )


def api_key_removed(public_key: str, name: str) -> None:
    _publish_event(
        _event(
            type="apiKey:removed",
            data={
                "apiKey": {
                    "publicKey": public_key,
                    "name": name,
                },
            },
        )
    )


def github_apps_updated() -> None:
    _publish_event(_event(type="github:apps:updated", data={}))


def github_repos_updated() -> None:
    _publish_event(_event(type="github:repos:updated", data={}))


def _event(type: str, data: dict[str, Any]) -> DiscoEvent:
    dt = datetime.now(timezone.utc)
    event = DiscoEvent(
        id=dt.isoformat(),
        timestamp=dt,
        type=type,
        data=data,
    )
    return event


def _publish_event(event: DiscoEvent) -> None:
    log.info(
        "Dispatching event %s: %s",
        event.type,
        json.dumps(
            {
                "id": event.id,
                "timestamp": event.timestamp.isoformat(),
                "type": event.type,
                "data": event.data,
            }
        ),
    )
    _events.append(event)
    while len(_events) > 0 and _events[0].timestamp < datetime.now(
        timezone.utc
    ) - timedelta(hours=1):
        _events.pop(0)
    for subscriber in _subscribers:
        subscriber.put_nowait(event)


def subscribe() -> asyncio.Queue[DiscoEvent]:
    log.info("Adding event subscriber")
    subscriber = asyncio.Queue[DiscoEvent]()
    _subscribers.append(subscriber)
    return subscriber


def unsubscribe(subscriber: asyncio.Queue[DiscoEvent]) -> None:
    if subscriber in _subscribers:
        log.info("Removing event subscriber")
        _subscribers.remove(subscriber)


def get_events_since(event_id: str) -> Iterable[DiscoEvent]:
    found = False
    for event in _events:
        if found:
            yield event
            continue
        assert not found
        if event.id == event_id:
            found = True
            continue
