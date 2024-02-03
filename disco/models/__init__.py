from sqlalchemy.orm import configure_mappers

from disco.models.apikey import ApiKey  # noqa: F401
from disco.models.commandoutput import CommandOutput  # noqa: F401
from disco.models.deployment import Deployment  # noqa: F401
from disco.models.deploymentenvironmentvariable import (
    DeploymentEnvironmentVariable,  # noqa: F401
)
from disco.models.keyvalue import KeyValue  # noqa: F401
from disco.models.project import Project  # noqa: F401
from disco.models.projectenvironmentvariable import (
    ProjectEnvironmentVariable,  # noqa: F401
)
from disco.models.task import Task  # noqa: F401

configure_mappers()
