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
            (Allow, "api_key", "create_deployment"),
        ]
