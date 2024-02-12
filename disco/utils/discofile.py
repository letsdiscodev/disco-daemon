from decimal import Decimal

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


class Service(BaseModel):
    image: Image = Image(dockerfile="Dockerfile", context=".", pull=None)
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
