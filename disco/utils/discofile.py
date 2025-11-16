from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class Volume(BaseModel):
    name: str
    destination_path: str = Field(..., alias="destinationPath")


class PublishedPort(BaseModel):
    published_as: int = Field(..., alias="publishedAs")
    from_container_port: int = Field(..., alias="fromContainerPort")
    protocol: str = "tcp"


class Image(BaseModel):
    dockerfile: str = "Dockerfile"
    context: str = "."


class ServiceType(str, Enum):
    container = "container"
    static = "static"
    generator = "generator"
    command = "command"
    cron = "cron"
    cgi = "cgi"


class Health(BaseModel):
    command: str


class Resources(BaseModel):
    cpu_limit: float | None = Field(None, alias="cpuLimit")
    memory_limit: str | None = Field(None, alias="memoryLimit")
    cpu_reservation: float | None = Field(None, alias="cpuReservation")
    memory_reservation: str | None = Field(None, alias="memoryReservation")


class Service(BaseModel):
    type: ServiceType = ServiceType.container
    public_path: str | None = Field(
        "dist",
        alias="publicPath",
    )
    image: str = "default"
    port: int = 8000
    command: str | None = None
    build: str | None = None
    published_ports: list[PublishedPort] = Field(
        [],
        alias="publishedPorts",
    )
    volumes: list[Volume] = []
    schedule: str = Field("* * * * *", pattern=r"^\*|\d+ \*|\d+ \*|\d+ \*|\d+ \*|\d+$")
    exposed_internally: bool = Field(
        False,
        alias="exposedInternally",
    )
    timeout: int = 300  # commands, static site generation, crons
    health: Health | None = None
    resources: Resources | None = None


class DiscoFile(BaseModel):
    version: Decimal
    services: dict[str, Service] = {}
    images: dict[str, Image] = {}


DEFAULT_DISCO_FILE = """{
    "version": "1.0",
    "services": {
        "web": {}
    }
}"""


def get_disco_file_from_str(disco_file_str: str | None) -> DiscoFile:
    if disco_file_str is None:
        disco_file_str = DEFAULT_DISCO_FILE
    disco_file = DiscoFile.model_validate_json(disco_file_str)
    if _should_add_default_image(disco_file):
        disco_file.images["default"] = Image(
            dockerfile="Dockerfile",
            context=".",
        )
    return disco_file


def _should_add_default_image(disco_file: DiscoFile) -> bool:
    if "default" in disco_file.images:
        # already defined
        return False
    for service in disco_file.services.values():
        if service.image != "default":
            continue
        if service.type == ServiceType.static and service.command is None:
            continue
        if service.build is not None:
            # uses build command, does not rely on images
            continue
        # at this point, it uses default and will execute something
        return True
    # no service used the default image, no need to add it
    return False
