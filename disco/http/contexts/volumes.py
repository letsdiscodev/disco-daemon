import logging

from pyramid.security import Allow

from disco.utils.docker import get_all_volumes

log = logging.getLogger(__name__)


class ListContext:
    def __init__(self, request):
        self.dbsession = request.dbsession

    @property
    def volume_names(self):
        return get_all_volumes()

    @property
    def __acl__(self):
        return [
            (Allow, "api_key", "get_volumes"),
            (Allow, "api_key", "create_volume"),
        ]


class SingleContext:
    def __init__(self, request):
        pass

    @property
    def __acl__(self):
        return [
            (Allow, "api_key", "delete_volume"),
        ]
