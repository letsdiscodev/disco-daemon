import logging

from pyramid.exceptions import HTTPNotFound
from pyramid.security import Allow

from disco.utils.projects import get_all_projects, get_project_by_id

log = logging.getLogger(__name__)


class ListContext:
    def __init__(self, request):
        self.dbsession = request.dbsession

    @property
    def projects(self):
        return get_all_projects(self.dbsession)

    @property
    def __acl__(self):
        return [
            (Allow, "api_key", "get_projects"),
            (Allow, "api_key", "create_project"),
        ]


class SingleByIdContext:
    def __init__(self, request):
        self.dbsession = request.dbsession
        self.project = get_project_by_id(
            request.dbsession, request.matchdict["project_id"]
        )
        if self.project is None:
            raise HTTPNotFound()

    @property
    def __acl__(self):
        return []
