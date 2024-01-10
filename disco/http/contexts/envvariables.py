import logging

from pyramid.httpexceptions import HTTPNotFound
from pyramid.security import Allow

from disco.utils.envvariables import (
    get_env_variable_by_name,
    get_env_variables_for_project,
)
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
    def env_variables(self):
        return get_env_variables_for_project(self.dbsession, self.project)

    @property
    def __acl__(self):
        return [
            (Allow, "api_key", "get_env_variables"),
            (Allow, "api_key", "set_env_variables"),
        ]


class SingleContext:
    def __init__(self, request):
        self.dbsession = request.dbsession
        self.project = get_project_by_name(
            request.dbsession, request.matchdict["project_name"]
        )
        if self.project is None:
            raise HTTPNotFound()

        self.env_variable = get_env_variable_by_name(
            dbsession=request.dbsession,
            project=self.project,
            name=request.matchdict["env_var_name"],
        )
        if self.env_variable is None:
            raise HTTPNotFound()

    @property
    def __acl__(self):
        return [
            (Allow, "api_key", "get_env_variable"),
            (Allow, "api_key", "update_env_variable"),
            (Allow, "api_key", "delete_env_variable"),
        ]
