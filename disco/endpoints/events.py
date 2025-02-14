import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_wo_tx
from disco.utils.events import DiscoEvent, get_events_since, subscribe, unsubscribe

log = logging.getLogger(__name__)

router = APIRouter()


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
            event=event.type,
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
            for event in get_events_since(last_event_id):
                yield sse_from_event(event)
        try:
            subscriber = subscribe()
            while True:
                event = await subscriber.get()
                yield sse_from_event(event)
        finally:
            unsubscribe(subscriber)

    return EventSourceResponse(get_events())
