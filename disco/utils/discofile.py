import re
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


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

    @field_validator("cpu_limit", "cpu_reservation")
    @classmethod
    def validate_cpu(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("CPU value must be positive (greater than 0)")
        return v

    @field_validator("memory_limit", "memory_reservation")
    @classmethod
    def validate_memory(cls, v: str | None) -> str | None:
        if v is not None:
            # Docker accepts memory in format: <number><unit>
            # Valid units: b, k, m, g (with optional 'b' suffix)
            # Examples: 256m, 1g, 512mb, 1024k
            if not re.match(r"^\d+[bkmg]b?$", v, re.IGNORECASE):
                raise ValueError(
                    "Memory value must be in format: <number><unit> "
                    "(e.g., '256m', '1g', '512mb'). Valid units: b, k, m, g"
                )
        return v

    @model_validator(mode="after")
    def validate_limits_and_reservations(self) -> "Resources":
        # Validate that limits are greater than or equal to reservations
        if self.cpu_limit is not None and self.cpu_reservation is not None:
            if self.cpu_limit < self.cpu_reservation:
                raise ValueError(
                    f"CPU limit ({self.cpu_limit}) must be greater than or equal to "
                    f"CPU reservation ({self.cpu_reservation})"
                )

        if self.memory_limit is not None and self.memory_reservation is not None:
            # Convert memory strings to bytes for comparison
            limit_bytes = self._memory_to_bytes(self.memory_limit)
            reservation_bytes = self._memory_to_bytes(self.memory_reservation)
            if limit_bytes < reservation_bytes:
                raise ValueError(
                    f"Memory limit ({self.memory_limit}) must be greater than or equal to "
                    f"memory reservation ({self.memory_reservation})"
                )

        return self

    @staticmethod
    def _memory_to_bytes(memory_str: str) -> int:
        """Convert memory string to bytes for comparison."""
        match = re.match(r"^(\d+)([bkmg])b?$", memory_str, re.IGNORECASE)
        if not match:
            raise ValueError(f"Invalid memory format: {memory_str}")

        value = int(match.group(1))
        unit = match.group(2).lower()

        multipliers = {
            "b": 1,
            "k": 1024,
            "m": 1024 * 1024,
            "g": 1024 * 1024 * 1024,
        }

        return value * multipliers[unit]


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
