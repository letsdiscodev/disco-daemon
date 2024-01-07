from disco.utils.projects import get_all_projects
from pyramid.security import Allow

import logging

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
