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
    dockerfile: str | None = None
    context: str | None = None
    pull: str | None = None


class ServiceType(str, Enum):
    container = "container"
    static = "static"


class Service(BaseModel):
    type: ServiceType = ServiceType.container
    # TODO validate that public_path starts with /
    public_path: str | None = Field(
        "/",
        alias="publicPath",
    )
    image: Image = Image()
    port: int = 8000
    command: str | None = None
    published_ports: list[PublishedPort] = Field(
        [],
        alias="publishedPorts",
    )
    volumes: list[Volume] = []


class DiscoFile(BaseModel):
    version: Decimal
    services: dict[str, Service] = {}
