import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_wo_tx

log = logging.getLogger(__name__)

router = APIRouter()


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


@router.get(
    "/api/disco/events",
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def events_get(
    last_event_id: Annotated[str | None, Header()] = None,
):
    def sse_from_event(event: DiscoEvent) -> ServerSentEvent:
        return ServerSentEvent(
            id=event.id,
            event="event",
            data=json.dumps(
                {
                    "id": event.id,
                    "timestamp": event.timestamp.isoformat(),
                    "type": event.type,
                    "data": event.data,
                }
            ),
        )

    async def get_events():
        if last_event_id is not None:
            found = False
            for event in _events:
                if not found and event.id != last_event_id:
                    continue
                yield sse_from_event(event)
        try:
            subscriber = asyncio.Queue[DiscoEvent]()
            _subscribers.append(subscriber)
            while True:
                event = await subscriber.get()
                yield sse_from_event(event)
        finally:
            if subscriber in _subscribers:
                _subscribers.remove(subscriber)

    return EventSourceResponse(get_events())
