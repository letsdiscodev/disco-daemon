import logging

from pyramid.httpexceptions import HTTPNotFound
from pyramid.security import Allow

from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)


class ListContext:
    def __init__(self, request):
        self.dbsession = request.dbsession
        self.project = get_project_by_name(
            request.dbsession, request.matchdict["project_name"]
        )
        if self.project is None:
            raise HTTPNotFound()

    @property
    def __acl__(self):
        return [
            (Allow, "api_key", "get_published_ports"),
            (Allow, "api_key", "add_published_ports"),
        ]


class SingleContext:
    def __init__(self, request):
        self.dbsession = request.dbsession
        self.project = get_project_by_name(
            request.dbsession, request.matchdict["project_name"]
        )
        if self.project is None:
            raise HTTPNotFound()
        try:
            self.published_port = [
                port
                for port in self.project.published_ports
                if str(port.host_port) == request.matchdict["host_port"]
                and port.protocol == request.matchdict["protocol"]
            ][0]
        except IndexError:
            raise HTTPNotFound()

    @property
    def __acl__(self):
        return [
            (Allow, "api_key", "remove_published_port"),
        ]
