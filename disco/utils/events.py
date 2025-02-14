import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


@dataclass
class DiscoEvent:
    id: str
    timestamp: datetime
    type: str
    data: dict[str, Any]


_subscribers: list[asyncio.Queue[DiscoEvent]] = []
_events: list[DiscoEvent] = []


def publish_event(event: DiscoEvent) -> None:
    _events.append(event)
    while len(_events) > 0 and _events[0].timestamp < datetime.now(
        timezone.utc
    ) - timedelta(hours=1):
        _events.pop(0)


def subscribe() -> asyncio.Queue[DiscoEvent]:
    subscriber = asyncio.Queue[DiscoEvent]()
    _subscribers.append(subscriber)
    return subscriber


def unsubscribe(subscriber: asyncio.Queue[DiscoEvent]) -> None:
    if subscriber in _subscribers:
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
