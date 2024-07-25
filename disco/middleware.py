import logging

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from disco.models.db import Session
from disco.utils.corsorigins import get_all_cors_origins_sync

log = logging.getLogger(__name__)

with Session.begin() as dbsession:
    cors_origins = get_all_cors_origins_sync(dbsession)
    allowed_origins = [o.origin for o in cors_origins]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
]


def update_cors(allowed_origins: list[str]) -> None:
    from disco.app import app

    log.info("Updating CORS allowed origin in middleware %s", allowed_origins)
    mw = app.middleware_stack
    while mw is not None:
        if isinstance(mw, CORSMiddleware):
            mw.allow_origins = allowed_origins
            log.info("CORS allowed origin in middleware updated")
            return
        mw = getattr(mw, "app", None)
